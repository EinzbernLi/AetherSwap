"""Trusted native Auto Offer module contract constants.

This package intentionally has no runtime registration or integration side
effects.  Runtime wiring belongs to a later task.
"""

AUTO_OFFER_MODULE_ID = "action.auto_offer_delivery"
AUTO_OFFER_STAGE = "buy.purchase_committed"
AUTO_OFFER_DEFAULT_ENABLED = False

__all__ = [
    "AUTO_OFFER_DEFAULT_ENABLED",
    "AUTO_OFFER_MODULE_ID",
    "AUTO_OFFER_STAGE",
]
