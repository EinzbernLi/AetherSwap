"""One-shot Requests adapter for Steam Community sent-history reads.

Credential material is injected by the caller and consumed only in memory. This
module does not know where credentials are stored and performs no logging,
persistence, retry, polling, redirect following, or Store/Host work.
"""

from __future__ import annotations

import re
import ssl
from typing import Callable
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.auto_offer.sent_offer_binding import SentOfferDiscoveryQuery
from app.auto_offer.steam_community_sent_history_transport import (
    COMMUNITY_SENT_HISTORY_REDIRECT_SUBTYPES,
    CommunitySentHistoryHttpResponse,
    CommunitySentHistoryNetworkError,
    CommunitySentHistoryRequestPlan,
    CommunitySentHistoryTransportError,
    build_community_sent_history_request,
)


_COOKIE_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
_AUTH_PATH_PREFIXES = ("/login", "/openid", "/oauth")


class CommunitySentHistoryRequestsError(CommunitySentHistoryTransportError):
    """Raised when the concrete one-shot sender cannot be constructed safely."""


def _parse_injected_cookies(cookies_raw: object) -> dict[str, str]:
    if type(cookies_raw) is not str or not cookies_raw:
        raise CommunitySentHistoryRequestsError("cookie_header_required")

    parsed: dict[str, str] = {}
    for raw_part in cookies_raw.split(";"):
        # Cookie headers conventionally place optional whitespace after ';'.
        # Remove that structural separator whitespace only; credential values
        # themselves are never stripped, normalized, hashed, or rewritten.
        part = raw_part.lstrip(" \t")
        if not part:
            continue
        if "=" not in part:
            raise CommunitySentHistoryRequestsError("malformed_cookie_part")
        raw_name, _, value = part.partition("=")
        name = raw_name.strip()
        if not name or _COOKIE_NAME_RE.fullmatch(name) is None:
            raise CommunitySentHistoryRequestsError("malformed_cookie_name")
        if raw_name != name:
            raise CommunitySentHistoryRequestsError("noncanonical_cookie_name")
        if value != value.strip():
            raise CommunitySentHistoryRequestsError("noncanonical_cookie_value")
        if name in parsed:
            raise CommunitySentHistoryRequestsError("duplicate_cookie_name")
        parsed[name] = value

    if not parsed.get("steamLoginSecure"):
        raise CommunitySentHistoryRequestsError("steam_login_secure_required")
    return parsed


def _zero_retry_policy() -> Retry:
    retry = Retry(
        total=0,
        connect=0,
        read=0,
        redirect=0,
        status=0,
        other=0,
        raise_on_redirect=False,
        raise_on_status=False,
    )
    for field in ("total", "connect", "read", "redirect", "status", "other"):
        if getattr(retry, field) != 0:
            raise CommunitySentHistoryRequestsError("retry_policy_not_zero")
    return retry


def _build_windows_trust_context() -> ssl.SSLContext:
    """Build a system-trust SSLContext without mutating global TLS behavior."""

    try:
        import truststore
    except Exception as exc:  # pragma: no cover - exercised by local capability gate.
        raise CommunitySentHistoryRequestsError("windows_truststore_unavailable") from exc

    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    return context


def _validate_ssl_context(context: object) -> ssl.SSLContext:
    if not isinstance(context, ssl.SSLContext):
        raise CommunitySentHistoryRequestsError("ssl_context_required")
    if context.verify_mode != ssl.CERT_REQUIRED:
        raise CommunitySentHistoryRequestsError("cert_required")
    if context.check_hostname is not True:
        raise CommunitySentHistoryRequestsError("hostname_check_required")
    return context


class WindowsTrustAdapter(HTTPAdapter):
    """HTTPS adapter that keeps one verified system-trust context end to end."""

    def __init__(self, ssl_context: ssl.SSLContext) -> None:
        self._ssl_context = _validate_ssl_context(ssl_context)
        super().__init__(max_retries=_zero_retry_policy())

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["ssl_context"] = self._ssl_context
        pool_kwargs["cert_reqs"] = ssl.CERT_REQUIRED
        return super().init_poolmanager(
            connections,
            maxsize,
            block=block,
            **pool_kwargs,
        )

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        # trust_env is disabled and session proxies are empty; this is a second
        # fail-safe in case a caller attempts to install a proxy explicitly.
        raise CommunitySentHistoryRequestsError("proxy_transport_forbidden")

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        """Preserve our context on Requests 2.32.x per-request pool keys."""

        parent = getattr(super(), "build_connection_pool_key_attributes", None)
        if not callable(parent):  # Requests < 2.32 does not call this hook.
            raise CommunitySentHistoryRequestsError("requests_pool_key_hook_unavailable")
        host_params, pool_kwargs = parent(request, verify, cert)
        pool_kwargs["ssl_context"] = self._ssl_context
        pool_kwargs["cert_reqs"] = ssl.CERT_REQUIRED
        return host_params, pool_kwargs


def _classify_request_exception(exc: BaseException) -> CommunitySentHistoryNetworkError:
    if isinstance(exc, requests.exceptions.SSLError):
        return CommunitySentHistoryNetworkError("tls")
    if isinstance(exc, requests.exceptions.Timeout):
        return CommunitySentHistoryNetworkError("timeout")
    return CommunitySentHistoryNetworkError("other")


def _normalized_origin(parsed) -> tuple[str, str, int] | None:
    try:
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname.lower() if parsed.hostname else ""
        port = parsed.port
    except (AttributeError, TypeError, ValueError):
        return None

    if scheme not in {"http", "https"} or not hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return (scheme, hostname, port)


def _classify_redirect_location(
    plan: CommunitySentHistoryRequestPlan,
    location: object,
) -> str:
    """Reduce an untrusted Location value to one bounded non-secret subtype."""

    if type(location) is not str or not location or location != location.strip():
        return "missing_or_invalid"
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in location):
        return "missing_or_invalid"

    try:
        expected = urlsplit(plan.url)
        destination = urlsplit(urljoin(plan.url, location))
    except (TypeError, ValueError):
        return "missing_or_invalid"

    expected_origin = _normalized_origin(expected)
    destination_origin = _normalized_origin(destination)
    if expected_origin is None or destination_origin is None:
        return "missing_or_invalid"
    if destination_origin != expected_origin:
        return "cross_origin"

    if destination.path == expected.path and destination.query == expected.query:
        return "same_target"

    profile_prefix, marker, _ = expected.path.partition("/tradeoffers/")
    if marker and profile_prefix:
        tradeoffers_root = f"{profile_prefix}/tradeoffers"
        if destination.path == tradeoffers_root or destination.path.startswith(
            f"{tradeoffers_root}/"
        ):
            return "same_profile_tradeoffers"

    destination_path = destination.path.lower()
    if any(
        destination_path == prefix or destination_path.startswith(f"{prefix}/")
        for prefix in _AUTH_PATH_PREFIXES
    ):
        return "steam_login_or_auth"

    return "same_origin_other"


class RequestsCommunitySentHistoryOneShotSender:
    """Single-use sender bound to one exact discovery query/request plan."""

    __slots__ = ("_expected_plan", "_session", "_used")

    def __init__(
        self,
        query: SentOfferDiscoveryQuery,
        cookies_raw: str,
        *,
        session_factory: Callable[[], requests.Session] = requests.Session,
        ssl_context_factory: Callable[[], ssl.SSLContext] = _build_windows_trust_context,
        adapter_factory: Callable[[ssl.SSLContext], HTTPAdapter] = WindowsTrustAdapter,
    ) -> None:
        if not callable(session_factory):
            raise CommunitySentHistoryRequestsError("session_factory_must_be_callable")
        if not callable(ssl_context_factory):
            raise CommunitySentHistoryRequestsError("ssl_context_factory_must_be_callable")
        if not callable(adapter_factory):
            raise CommunitySentHistoryRequestsError("adapter_factory_must_be_callable")

        # Bind the credential-bearing sender to the exact account/profile URL
        # derived from the validated local discovery query before a session can
        # become dispatch-capable. Hand-crafted alternate URLs are rejected.
        expected_plan = build_community_sent_history_request(query)
        cookies = _parse_injected_cookies(cookies_raw)
        context = _validate_ssl_context(ssl_context_factory())
        adapter = adapter_factory(context)
        if not isinstance(adapter, HTTPAdapter):
            raise CommunitySentHistoryRequestsError("http_adapter_required")

        session = session_factory()
        if session is None:
            raise CommunitySentHistoryRequestsError("session_factory_returned_none")
        if not hasattr(session, "mount") or not callable(session.mount):
            raise CommunitySentHistoryRequestsError("session_mount_required")

        session.trust_env = False
        session.verify = True
        proxies = getattr(session, "proxies", None)
        if proxies is None or not hasattr(proxies, "clear"):
            raise CommunitySentHistoryRequestsError("session_proxies_required")
        proxies.clear()
        session.mount("https://", adapter)
        session.cookies.update(cookies)
        session.headers.update(
            {
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Referer": "https://steamcommunity.com/",
            }
        )
        self._expected_plan = expected_plan
        self._session = session
        self._used = False

    def __call__(
        self,
        plan: CommunitySentHistoryRequestPlan,
    ) -> CommunitySentHistoryHttpResponse:
        if type(plan) is not CommunitySentHistoryRequestPlan:
            raise CommunitySentHistoryRequestsError("invalid_request_plan")
        CommunitySentHistoryRequestPlan.__post_init__(plan)
        if plan != self._expected_plan:
            raise CommunitySentHistoryRequestsError("request_plan_identity_mismatch")
        if self._used:
            raise CommunitySentHistoryRequestsError("sender_already_used")
        self._used = True

        try:
            response = self._session.get(
                plan.url,
                timeout=plan.timeout,
                allow_redirects=plan.allow_redirects,
                verify=plan.verify_tls,
            )
        except Exception as exc:
            raise _classify_request_exception(exc) from None

        status_code = getattr(response, "status_code", None)
        headers = getattr(response, "headers", None)
        text = getattr(response, "text", None)
        if type(status_code) is not int or not 100 <= status_code <= 599:
            raise CommunitySentHistoryRequestsError("invalid_response_status")
        if headers is None or not hasattr(headers, "get"):
            raise CommunitySentHistoryRequestsError("invalid_response_headers")
        if type(text) is not str:
            raise CommunitySentHistoryRequestsError("invalid_response_text")

        content_type = headers.get("Content-Type", "")
        if type(content_type) is not str:
            raise CommunitySentHistoryRequestsError("invalid_content_type")

        redirect_subtype = "none"
        if 300 <= status_code <= 399:
            redirect_subtype = _classify_redirect_location(plan, headers.get("Location"))
        if redirect_subtype not in COMMUNITY_SENT_HISTORY_REDIRECT_SUBTYPES:
            raise CommunitySentHistoryRequestsError("invalid_redirect_subtype")

        return CommunitySentHistoryHttpResponse(
            status_code=status_code,
            content_type=content_type,
            body=text,
            redirect_subtype=redirect_subtype,
        )

    def close(self) -> None:
        close = getattr(self._session, "close", None)
        if callable(close):
            close()


__all__ = [
    "CommunitySentHistoryRequestsError",
    "RequestsCommunitySentHistoryOneShotSender",
    "WindowsTrustAdapter",
]
