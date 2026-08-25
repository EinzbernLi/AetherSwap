"""Disabled compatibility entry for the retired legacy confirmer.

Listing confirmation is intentionally not implemented here.  Future listing
confirmation work must introduce a separately reviewed exact-identity
contract; this module must remain side-effect free.
"""

from typing import Any


LEGACY_CONFIRMATION_DISABLED = "legacy_bulk_confirmation_disabled"


def auto_confirm_once(
    identity_secret: str,
    device_id: str,
    steam_id: str,
    cookies: Any,
) -> tuple[bool, int, str]:
    """Return a stable fail-closed result without reading any argument.

    The parameter names remain for compatibility with historical callers, but
    no credential, cookie, session, or network operation is performed.
    """

    return False, 0, LEGACY_CONFIRMATION_DISABLED
