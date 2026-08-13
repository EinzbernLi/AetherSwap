"""Pure exact counterparty evidence helpers for Auto Offer reads.

The helpers normalize no fuzzy aliases and perform no I/O or persistence.
They only accept seller Steam identity fields already present in one exact
BUFF order record.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


_SELLER_FIELDS = ("seller_steam_id", "seller_steamid")


class CounterpartyEvidenceError(ValueError):
    pass


def _exact_identifier(value: object, *, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise CounterpartyEvidenceError(f"invalid_{field}")
    return value


@dataclass(frozen=True)
class SellerCounterpartyEvidence:
    steam_id: str

    def __post_init__(self) -> None:
        _exact_identifier(self.steam_id, field="seller_steam_id")


def seller_counterparty_from_exact_buff_record(
    record: Mapping[str, object],
) -> SellerCounterpartyEvidence:
    """Require one unambiguous seller SteamID from an exact BUFF order row."""

    if not isinstance(record, Mapping):
        raise CounterpartyEvidenceError("invalid_record")

    values: list[str] = []
    for field in _SELLER_FIELDS:
        if field not in record:
            continue
        values.append(_exact_identifier(record[field], field=field))

    if not values:
        raise CounterpartyEvidenceError("seller_steam_id_not_proven")
    if len(set(values)) != 1:
        raise CounterpartyEvidenceError("seller_steam_id_conflict")
    return SellerCounterpartyEvidence(values[0])


__all__ = [
    "CounterpartyEvidenceError",
    "SellerCounterpartyEvidence",
    "seller_counterparty_from_exact_buff_record",
]
