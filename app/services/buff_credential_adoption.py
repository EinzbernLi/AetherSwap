from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from app.config_loader import (
    get_buff_credentials,
    load_app_config_validated,
    update_buff_creds,
)
from app.services.buff_auth import (
    buff_credential_replacement_block_reason,
    get_buff_auth_lock,
)
from app.services.buff_client import BuffClient
from app.services.buff_egress import (
    BuffEgressBinding,
    BuffEgressError,
    BuffEgressReauthRequired,
    resolve_buff_egress,
    validate_buff_credential_binding,
)


@dataclass(frozen=True)
class LegacyBuffCredentialAdoptionResult:
    """Secret-free result of one explicit legacy credential adoption attempt."""

    ok: bool
    status: str
    message: str = ""
    binding_mode: str = ""
    generation_before: int = 0
    generation_after: int = 0

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "status": self.status,
            "message": self.message,
            "binding_mode": self.binding_mode,
            "generation_before": self.generation_before,
            "generation_after": self.generation_after,
        }


def _generation(credentials: dict) -> int:
    try:
        return int((credentials or {}).get("generation", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _binding_metadata_state(credentials: dict) -> str:
    creds = credentials if isinstance(credentials, dict) else {}
    mode = str(creds.get("egress_mode") or "").strip()
    fingerprint = str(creds.get("egress_fingerprint") or "").strip()
    if not mode and not fingerprint:
        return "legacy_unbound"
    if not mode or not fingerprint:
        return "invalid"
    return "present"


def _credential_preimage(credentials: dict) -> tuple:
    creds = credentials if isinstance(credentials, dict) else {}
    return (
        _generation(creds),
        str(creds.get("cookies") or ""),
        str(creds.get("user_agent") or ""),
        str(creds.get("egress_mode") or ""),
        str(creds.get("egress_fingerprint") or ""),
    )


def _classify_validation_exception(exc: Exception) -> tuple[str, str]:
    try:
        from buff import (
            BuffAuthExpired,
            BuffRateLimited,
            BuffRequestBlocked,
            BuffRiskControlTriggered,
            BuffVerificationRequired,
        )

        if isinstance(exc, BuffAuthExpired):
            return "expired", "BUFF 登录状态已失效"
        if isinstance(exc, BuffRateLimited):
            return "rate_limited", "BUFF 当前处于服务器限流冷却期"
        if isinstance(exc, (BuffVerificationRequired, BuffRiskControlTriggered)):
            return "verification_required", "BUFF 要求安全验证"
        if isinstance(exc, BuffRequestBlocked):
            return "request_blocked", "BUFF 请求策略当前阻止会话验证"
    except Exception:
        pass
    return "validation_error", f"BUFF 只读会话验证失败: {type(exc).__name__}"


def adopt_legacy_buff_credentials_for_current_egress(
    *,
    config: Optional[dict] = None,
    client_factory: Optional[Callable[..., BuffClient]] = None,
) -> LegacyBuffCredentialAdoptionResult:
    """Bind existing metadata-free BUFF credentials after one read-only proof.

    This operation is intentionally explicit.  It never runs as a hidden side
    effect of pipeline start or ordinary BuffClient construction.

    Existing credentials are sent through the currently resolved immutable
    BuffEgressBinding and verified by the existing BuffClient.verify_session()
    read path.  Egress metadata is persisted only after that proof succeeds.
    Any expired/risk/rate-limit/transport/identity ambiguity leaves the saved
    credential generation untouched so the ordinary browser/manual auth path
    remains the recovery fallback.
    """

    with get_buff_auth_lock():
        replacement_block = buff_credential_replacement_block_reason()
        if replacement_block:
            return LegacyBuffCredentialAdoptionResult(
                False,
                "credential_frozen",
                replacement_block,
            )

        credentials = get_buff_credentials() or {}
        generation_before = _generation(credentials)
        metadata_state = _binding_metadata_state(credentials)

        cfg = load_app_config_validated() if config is None else config
        try:
            binding = resolve_buff_egress(cfg)
        except BuffEgressError as exc:
            return LegacyBuffCredentialAdoptionResult(
                False,
                "egress_unavailable",
                exc.code,
                generation_before=generation_before,
            )

        if metadata_state == "invalid":
            return LegacyBuffCredentialAdoptionResult(
                False,
                "binding_invalid",
                "BUFF egress metadata 不完整或无效",
                binding_mode=binding.mode,
                generation_before=generation_before,
            )

        if metadata_state == "present":
            try:
                validate_buff_credential_binding(credentials, binding)
            except BuffEgressReauthRequired as exc:
                return LegacyBuffCredentialAdoptionResult(
                    False,
                    "reauth_required",
                    exc.code,
                    binding_mode=binding.mode,
                    generation_before=generation_before,
                )
            return LegacyBuffCredentialAdoptionResult(
                True,
                "already_bound",
                binding_mode=binding.mode,
                generation_before=generation_before,
                generation_after=generation_before,
            )

        cookies = str(credentials.get("cookies") or "")
        user_agent = str(credentials.get("user_agent") or "").strip() or None
        if not cookies:
            return LegacyBuffCredentialAdoptionResult(
                False,
                "expired",
                "BUFF Cookie 为空，无法复用现有登录态",
                binding_mode=binding.mode,
                generation_before=generation_before,
            )

        credential_preimage = _credential_preimage(credentials)
        validated = {
            "cookies": cookies,
            "user_agent": user_agent,
        }

        def capture_rotated_credentials(latest_cookies: str, latest_ua: str) -> None:
            if latest_cookies:
                validated["cookies"] = latest_cookies
            normalized_ua = str(latest_ua or "").strip()
            if normalized_ua:
                validated["user_agent"] = normalized_ua

        factory = client_factory or BuffClient
        client = factory(
            cookies,
            user_agent=user_agent,
            credential_generation=generation_before,
            credentials_update_callback=capture_rotated_credentials,
            egress_binding=binding,
        )
        try:
            try:
                verified = bool(client.verify_session())
            except Exception as exc:
                status, message = _classify_validation_exception(exc)
                return LegacyBuffCredentialAdoptionResult(
                    False,
                    status,
                    message,
                    binding_mode=binding.mode,
                    generation_before=generation_before,
                )
        finally:
            try:
                client.close()
            except Exception:
                pass

        if not verified:
            return LegacyBuffCredentialAdoptionResult(
                False,
                "not_verified",
                "现有 BUFF 登录态未通过只读验证；请使用正常登录流程重新验证",
                binding_mode=binding.mode,
                generation_before=generation_before,
            )

        # Do not attach an egress identity to a different credential generation
        # if another writer changed Cookie/UA/metadata while the network proof
        # was in flight.
        current = get_buff_credentials() or {}
        if _credential_preimage(current) != credential_preimage:
            return LegacyBuffCredentialAdoptionResult(
                False,
                "credential_changed",
                "BUFF 凭据在只读验证期间发生变化，已拒绝绑定",
                binding_mode=binding.mode,
                generation_before=generation_before,
            )

        update_buff_creds(
            str(validated.get("cookies") or cookies),
            user_agent=(str(validated.get("user_agent") or "").strip() or None),
            egress_mode=binding.mode,
            egress_fingerprint=binding.fingerprint,
        )

        committed = get_buff_credentials() or {}
        generation_after = _generation(committed)
        try:
            binding_status = validate_buff_credential_binding(committed, binding)
        except BuffEgressReauthRequired:
            return LegacyBuffCredentialAdoptionResult(
                False,
                "commit_mismatch",
                "BUFF 凭据写入后 egress binding 校验失败",
                binding_mode=binding.mode,
                generation_before=generation_before,
                generation_after=generation_after,
            )

        expected_generation = generation_before + 1
        if binding_status != "bound" or generation_after != expected_generation:
            return LegacyBuffCredentialAdoptionResult(
                False,
                "commit_mismatch",
                "BUFF 凭据 adoption generation 校验失败",
                binding_mode=binding.mode,
                generation_before=generation_before,
                generation_after=generation_after,
            )

        return LegacyBuffCredentialAdoptionResult(
            True,
            "adopted",
            binding_mode=binding.mode,
            generation_before=generation_before,
            generation_after=generation_after,
        )
