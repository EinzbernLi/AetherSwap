from __future__ import annotations

from fnmatch import fnmatchcase
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


def _host_matches_bypass_token(host: str, raw_token: object) -> bool:
    """Match one no-proxy/ProxyOverride token without DNS or socket access."""

    normalized_host = str(host or "").strip().lower().rstrip(".")
    token = str(raw_token or "").strip().lower()
    if not normalized_host or not token:
        return False
    if token == "*":
        return True
    if token == "<local>":
        return "." not in normalized_host

    # no_proxy values sometimes include a URL form or an explicit port.  Only
    # the host identity matters for the fixed BUFF origin used by this module.
    if "://" in token:
        try:
            token = str(urlsplit(token).hostname or "").lower()
        except Exception:
            return False
    else:
        port_match = re.fullmatch(r"(.+):(\d+)", token)
        if port_match:
            token = port_match.group(1)

    token = token.strip().rstrip(".")
    if not token:
        return False
    if token == "*":
        return True
    if token == "<local>":
        return "." not in normalized_host

    if "*" in token or "?" in token:
        return fnmatchcase(normalized_host, token)

    token = token.lstrip(".")
    if not token:
        return False
    return normalized_host == token or normalized_host.endswith(f".{token}")


def _proxy_snapshot_bypasses_host(
    resolved: Mapping[str, object],
    host: str,
) -> bool:
    """Evaluate bypass rules from the already-resolved ambient proxy snapshot."""

    raw_no_proxy = resolved.get("no")
    if raw_no_proxy is None:
        raw_no_proxy = resolved.get("no_proxy")
    if raw_no_proxy is None:
        return False

    for token in re.split(r"[;,]", str(raw_no_proxy)):
        if _host_matches_bypass_token(host, token):
            return True
    return False


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
    try:
        resolved = resolver() or {}
        normalized = {
            str(key).strip().lower(): value for key, value in resolved.items()
        }
        bypassed = (
            bool(bypass_resolver(BUFF_HOST))
            if bypass_resolver is not None
            else _proxy_snapshot_bypasses_host(normalized, BUFF_HOST)
        )
        if bypassed:
            raise BuffEgressError("BUFF_EGRESS_SYSTEM_PROXY_BYPASS")
    except BuffEgressError:
        raise
    except Exception:
        raise BuffEgressError("BUFF_EGRESS_SYSTEM_PROXY_RESOLUTION_FAILED") from None

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
