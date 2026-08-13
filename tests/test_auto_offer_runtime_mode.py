from __future__ import annotations

import pytest

from app.auto_offer.runtime_mode import (
    AutoOfferRuntimeMode,
    AutoOfferRuntimeState,
    resolve_runtime_mode,
)


def test_runtime_mode_values_are_exact():
    assert [item.value for item in AutoOfferRuntimeMode] == [
        "off",
        "enabling",
        "on",
        "draining",
        "blocked",
    ]


def test_disabled_without_active_ownership_is_off():
    state = resolve_runtime_mode(
        requested_enabled=False,
        active_delivery_count=0,
    )
    assert state == AutoOfferRuntimeState(
        requested_enabled=False,
        active_delivery_count=0,
        mode=AutoOfferRuntimeMode.OFF,
    )


def test_enabled_request_is_on_when_no_transition_block_exists():
    state = resolve_runtime_mode(
        requested_enabled=True,
        active_delivery_count=3,
    )
    assert state.mode is AutoOfferRuntimeMode.ON
    assert state.requested_enabled is True
    assert state.active_delivery_count == 3
    assert state.reason is None


def test_disable_request_with_active_ownership_is_draining():
    state = resolve_runtime_mode(
        requested_enabled=False,
        active_delivery_count=2,
    )
    assert state.mode is AutoOfferRuntimeMode.DRAINING
    assert state.requested_enabled is False
    assert state.active_delivery_count == 2
    assert state.reason is None


def test_transition_block_reason_is_explicit_and_does_not_change_request():
    state = resolve_runtime_mode(
        requested_enabled=True,
        active_delivery_count=0,
        transition_block_reason="legacy_pending_unowned",
    )
    assert state.mode is AutoOfferRuntimeMode.BLOCKED
    assert state.requested_enabled is True
    assert state.reason == "legacy_pending_unowned"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"requested_enabled": 1, "active_delivery_count": 0},
        {"requested_enabled": False, "active_delivery_count": -1},
        {"requested_enabled": False, "active_delivery_count": True},
        {
            "requested_enabled": False,
            "active_delivery_count": 0,
            "transition_block_reason": " bad",
        },
    ],
)
def test_invalid_runtime_mode_inputs_fail_closed(kwargs):
    with pytest.raises((TypeError, ValueError)):
        resolve_runtime_mode(**kwargs)
