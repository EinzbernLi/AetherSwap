"""Canary-only BUFF purchase adapter that forbids multi-order batch writes.

The normal purchase implementation remains authoritative. This wrapper only
forces its existing safe single-order fallback while a one-shot canary takeover
is PREPARED, so one call cannot durably create multiple Host purchases before
the canary owner fence is established.
"""

from __future__ import annotations


class CanarySinglePurchaseBuffClient:
    """Delegate all BUFF behavior except batch-purchase capability."""

    supports_batch_buy = False

    def __init__(self, delegate) -> None:
        if delegate is None:
            raise ValueError("canary_purchase_client_missing")
        self._delegate = delegate

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)


__all__ = ["CanarySinglePurchaseBuffClient"]
