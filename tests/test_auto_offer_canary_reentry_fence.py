from __future__ import annotations

import pytest

import app.pipeline as pipeline
from app.auto_offer.canary_takeover import (
    CanaryTakeover,
    CanaryTakeoverIntegration,
    CanaryTakeoverPhase,
)


class _NormalIntegration:
    account_id = "account"
    recipient_steam_id = "76561198000000101"
    registration_enabled = True

    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class _State:
    @staticmethod
    def complete_purchase_receipt_by_id(*_args):
        return True

    @staticmethod
    def delete_refund_cleanup_purchase(*_args):
        return True


class _Context:
    state = _State()


def _controller_at(phase: CanaryTakeoverPhase) -> CanaryTakeover:
    controller = CanaryTakeover(
        host_purchases_provider=lambda: [],
        store_rows_provider=lambda: [],
        checkout_provider=lambda: None,
    )
    with controller._lock:
        controller._phase = phase
    return controller


@pytest.mark.parametrize(
    "phase",
    [
        CanaryTakeoverPhase.PREPARED,
        CanaryTakeoverPhase.TARGET_CAPTURED,
        CanaryTakeoverPhase.OWNER_ACTIVE,
        CanaryTakeoverPhase.COMPLETE,
    ],
)
def test_pipeline_wrapper_admission_remains_active_across_one_purchase_fence(
    monkeypatch,
    phase,
):
    controller = _controller_at(phase)
    normal = _NormalIntegration()
    seen = []

    monkeypatch.setattr(
        pipeline,
        "get_canary_takeover",
        lambda: controller,
    )
    monkeypatch.setattr(
        pipeline,
        "build_host_auto_offer_integration",
        lambda **_kwargs: normal,
    )

    def impl(*_args, **kwargs):
        seen.append(kwargs["auto_offer_integration"])
        return 0.0, 0, True

    monkeypatch.setattr(
        pipeline,
        "_process_deals_for_target_impl",
        impl,
    )

    result = pipeline._process_deals_for_target(
        _Context(),
        [],
        {"auto_offer": {"enabled": True}},
        10.0,
        0.0,
        0,
        object(),
        object(),
        object(),
        set(),
        set(),
        set(),
    )

    assert result == (0.0, 0, True)
    assert len(seen) == 1
    assert isinstance(seen[0], CanaryTakeoverIntegration)
    assert seen[0]._controller is controller
    assert normal.closed == 1


@pytest.mark.parametrize(
    "phase",
    [CanaryTakeoverPhase.IDLE, CanaryTakeoverPhase.ABORTED],
)
def test_pipeline_wrapper_admission_is_not_reopened_outside_live_canary(
    phase,
):
    controller = _controller_at(phase)
    assert controller.is_prepared is False
