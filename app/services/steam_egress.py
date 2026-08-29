"""Source-owned Steam HTTP egress for strict Auto Offer operations.

This module owns only route/session mechanics. Callers retain endpoint allowlists,
request semantics, parsing, retry policy, and platform-result interpretation.
Each facade call selects one Aether route and performs exactly one HTTP attempt.
"""

from __future__ import annotations

from typing import Any

import requests

from utils.proxy_manager import get_proxy_manager


class SteamHostEgressError(RuntimeError):
    """Fail-closed Host Steam egress configuration or lifecycle error."""


class SteamHostEgressSession:
    """Requests-like Host facade with one source-selected route per call."""

    __slots__ = ("_session", "_proxy_manager", "_closed")

    def __init__(self, *, session: object | None = None, proxy_manager: object | None = None) -> None:
        owns_session = session is None
        client = requests.Session() if owns_session else session
        if client is None:
            raise SteamHostEgressError("steam_egress_session_required")
        if getattr(client, "verify", None) is False:
            raise SteamHostEgressError("steam_egress_tls_verification_disabled")
        for method in ("get", "post", "close"):
            if not callable(getattr(client, method, None)):
                raise SteamHostEgressError("steam_egress_session_invalid")
        if owns_session:
            try:
                client.trust_env = False
            except Exception:
                raise SteamHostEgressError("steam_egress_trust_env_uncontrolled") from None
            if getattr(client, "trust_env", None) is not False:
                raise SteamHostEgressError("steam_egress_trust_env_uncontrolled")

        manager = get_proxy_manager() if proxy_manager is None else proxy_manager
        if not callable(getattr(manager, "get_proxies_for_request", None)):
            if owns_session:
                try:
                    client.close()
                except Exception:
                    pass
            raise SteamHostEgressError("steam_egress_proxy_manager_invalid")

        self._session = client
        self._proxy_manager = manager
        self._closed = False

    @property
    def verify(self) -> object:
        value = getattr(self._session, "verify", True)
        if value is False:
            raise SteamHostEgressError("steam_egress_tls_verification_disabled")
        return value

    def _request(self, method: str, url: object, kwargs: dict[str, Any]) -> object:
        if self._closed:
            raise SteamHostEgressError("steam_egress_closed")
        if "proxies" in kwargs:
            raise SteamHostEgressError("steam_egress_proxies_forbidden")
        if getattr(self._session, "verify", None) is False:
            raise SteamHostEgressError("steam_egress_tls_verification_disabled")

        try:
            proxies = self._proxy_manager.get_proxies_for_request(failed=False)
        except Exception:
            raise SteamHostEgressError("steam_egress_route_selection_failed") from None

        request = getattr(self._session, method)
        return request(url, proxies=proxies, **kwargs)

    def get(self, url: object, **kwargs: Any) -> object:
        return self._request("get", url, dict(kwargs))

    def post(self, url: object, **kwargs: Any) -> object:
        return self._request("post", url, dict(kwargs))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._session.close()


__all__ = ["SteamHostEgressError", "SteamHostEgressSession"]
