from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional
from urllib.parse import urlsplit
from urllib.request import getproxies

BUFF_ORIGIN = "https://buff.163.com/"
BUFF_HOST = "buff.163.com"
BUFF_EGRESS_DIRECT = "direct"
BUFF_EGRESS_SYSTEM_PROXY = "system_proxy"
BUFF_EGRESS_MODES = frozenset({BUFF_EGRESS_DIRECT, BUFF_EGRESS_SYSTEM_PROXY})

_PROXY_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_BROWSER_BASE_ARGS = ("--disable-blink-features=AutomationControlled",)


class BuffEgressError(RuntimeError):
    """A local egress configuration error that must stop before BUFF traffic."""

    def __init__(self, code: str):
        self.code = str(code or "BUFF_EGRESS_ERROR")
        super().__init__(self.code)


class BuffEgressReauthRequired(BuffEgressError):
    """The saved BUFF credential generation belongs to another egress."""


@dataclass(frozen=True)
class BuffEgressBinding:
    """Immutable route identity shared by browser auth and requests traffic."""

    mode: str
    fingerprint: str
    _proxy_server: Optional[str] = field(default=None, repr=False)

    @property
    def is_direct(self) -> bool:
        return self.mode == BUFF_EGRESS_DIRECT

    def browser_launch_args(self) -> list[str]:
        args = list(_BROWSER_BASE_ARGS)
        if self.is_direct:
            args.append("--no-proxy-server")
        else:
            if not self._proxy_server:
                raise BuffEgressError("BUFF_EGRESS_BINDING_INVALID")
            args.append(f"--proxy-server={self._proxy_server}")
        return args

    def requests_proxies(self) -> dict[str, str]:
        if self.is_direct:
            return {}
        if not self._proxy_server:
            raise BuffEgressError("BUFF_EGRESS_BINDING_INVALID")
        return {"http": self._proxy_server, "https": self._proxy_server}

    def sanitized_status(self) -> dict[str, str]:
        return {
            "mode": self.mode,
            "binding": "direct" if self.is_direct else "proxy",
        }


def normalize_buff_egress_mode(value: object) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in BUFF_EGRESS_MODES else BUFF_EGRESS_DIRECT


def _fingerprint(mode: str, proxy_server: str = "") -> str:
    canonical = f"aetherswap-buff-egress-v1\n{mode}\n{proxy_server}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def direct_buff_egress_binding() -> BuffEgressBinding:
    return BuffEgressBinding(
        mode=BUFF_EGRESS_DIRECT,
        fingerprint=_fingerprint(BUFF_EGRESS_DIRECT),
    )


def _normalize_system_proxy_url(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        raise BuffEgressError("BUFF_EGRESS_SYSTEM_PROXY_UNAVAILABLE")
    if "://" not in text:
        text = f"http://{text}"
    try:
        parsed = urlsplit(text)
        scheme = str(parsed.scheme or "").lower()
        if scheme not in {"http", "https"}:
            raise BuffEgressError("BUFF_EGRESS_SYSTEM_PROXY_SCHEME_UNSUPPORTED")
        if parsed.username is not None or parsed.password is not None:
            # Passing authenticated proxy material through Chromium command-line
            # arguments would expose it to the local process list.  V1 therefore
            # fails closed instead of weakening secret handling.
            raise BuffEgressError("BUFF_EGRESS_SYSTEM_PROXY_AUTH_UNSUPPORTED")
        if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise BuffEgressError("BUFF_EGRESS_SYSTEM_PROXY_INVALID")
        host = parsed.hostname
        if not host:
            raise BuffEgressError("BUFF_EGRESS_SYSTEM_PROXY_INVALID")
        port = parsed.port or (443 if scheme == "https" else 80)
    except BuffEgressError:
        raise
    except Exception:
        raise BuffEgressError("BUFF_EGRESS_SYSTEM_PROXY_INVALID") from None

    normalized_host = host.lower()
    if ":" in normalized_host and not normalized_host.startswith("["):
        normalized_host = f"[{normalized_host}]"
    return f"{scheme}://{normalized_host}:{int(port)}"


def _default_proxy_bypass(host: str) -> bool:
    """Return the ambient bypass decision without Windows DNS resolution.

    CPython's Windows ``urllib.request.proxy_bypass`` registry path may resolve
    the host name while evaluating ``ProxyOverride``.  That violates the BUFF
    egress contract because route resolution must be local-only before the first
    intended browser/HTTP network operation.  Requests has long provided a
    Windows-specific override that preserves environment/Registry bypass
    semantics without those DNS lookups, so use that reviewed compatibility
    seam as the production default.
    """

    from requests.utils import proxy_bypass as requests_proxy_bypass

    return bool(requests_proxy_bypass(host))


def resolve_buff_egress(
    config: Optional[dict] = None,
    *,
    proxy_resolver: Optional[Callable[[], Mapping[str, object]]] = None,
    bypass_resolver: Optional[Callable[[str], bool]] = None,
) -> BuffEgressBinding:
    cfg = config if isinstance(config, dict) else {}
    buff_cfg = cfg.get("buff") if isinstance(cfg.get("buff"), dict) else {}
    mode = normalize_buff_egress_mode(buff_cfg.get("egress_mode"))
    if mode == BUFF_EGRESS_DIRECT:
        return direct_buff_egress_binding()

    resolver = proxy_resolver or getproxies
    bypass = bypass_resolver or _default_proxy_bypass
    try:
        if bool(bypass(BUFF_HOST)):
            raise BuffEgressError("BUFF_EGRESS_SYSTEM_PROXY_BYPASS")
        resolved = resolver() or {}
    except BuffEgressError:
        raise
    except Exception:
        raise BuffEgressError("BUFF_EGRESS_SYSTEM_PROXY_RESOLUTION_FAILED") from None

    normalized = {str(key).strip().lower(): value for key, value in resolved.items()}
    raw_proxy = normalized.get("https") or normalized.get("http")
    proxy_server = _normalize_system_proxy_url(raw_proxy)
    return BuffEgressBinding(
        mode=BUFF_EGRESS_SYSTEM_PROXY,
        fingerprint=_fingerprint(BUFF_EGRESS_SYSTEM_PROXY, proxy_server),
        _proxy_server=proxy_server,
    )


def configure_requests_session(session, binding: BuffEgressBinding):
    """Apply one immutable explicit egress to a requests.Session."""

    session.trust_env = False
    session.proxies.clear()
    session.proxies.update(binding.requests_proxies())
    return session


def credential_binding_metadata(binding: BuffEgressBinding) -> dict[str, str]:
    return {
        "egress_mode": binding.mode,
        "egress_fingerprint": binding.fingerprint,
    }


def validate_buff_credential_binding(
    credentials: Optional[dict],
    binding: BuffEgressBinding,
) -> str:
    """Fail closed if saved credentials were authenticated on another route."""

    creds = credentials if isinstance(credentials, dict) else {}
    saved_mode = str(creds.get("egress_mode") or "").strip().lower()
    saved_fingerprint = str(creds.get("egress_fingerprint") or "").strip().lower()

    if not saved_mode and not saved_fingerprint:
        if binding.mode == BUFF_EGRESS_DIRECT:
            # Pre-TASK-055 credentials were produced under the hard-coded direct
            # contract, so direct mode is the only safe legacy adoption.
            return "legacy_direct"
        raise BuffEgressReauthRequired("BUFF_EGRESS_REAUTH_REQUIRED")

    if (
        saved_mode not in BUFF_EGRESS_MODES
        or not _PROXY_FINGERPRINT_RE.fullmatch(saved_fingerprint)
    ):
        raise BuffEgressReauthRequired("BUFF_EGRESS_BINDING_INVALID")
    if saved_mode != binding.mode or saved_fingerprint != binding.fingerprint:
        raise BuffEgressReauthRequired("BUFF_EGRESS_REAUTH_REQUIRED")
    return "bound"
