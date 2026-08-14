from __future__ import annotations

import threading
from contextlib import contextmanager

import pytest

import app.pipeline as pipeline
from app.routes import config as config_route


class _State:
    def __init__(self) -> None:
        self.statuses = []

    def set_status(self, *args, **kwargs) -> None:
        self.statuses.append((args, kwargs))


@contextmanager
def _activity_guard():
    yield


def _install_start_environment(monkeypatch, *, persisted_enabled: bool):
    from app.services import buff_auth

    monkeypatch.setattr(buff_auth, "get_buff_auth_lock", lambda: threading.RLock())
    monkeypatch.setattr(pipeline, "buff_activity_guard", _activity_guard)
    monkeypatch.setattr(pipeline, "get_unresolved_checkout", lambda: None)
    monkeypatch.setattr(pipeline, "get_pipeline_start_blocker", lambda: {})
    monkeypatch.setattr(pipeline, "get_state", lambda: _State())
    monkeypatch.setattr(
        pipeline,
        "load_app_config_validated",
        lambda: {"auto_offer": {"enabled": persisted_enabled}},
    )
    monkeypatch.setattr(pipeline, "_pipeline_maintenance_reason", "")
    monkeypatch.setattr(pipeline, "_shutdown_pending", False)
    with pipeline._pipeline_start_lock:
        pipeline._pipeline_thread = None


def _start_and_capture(monkeypatch, caller_config, *, persisted_enabled: bool):
    _install_start_environment(monkeypatch, persisted_enabled=persisted_enabled)
    started = threading.Event()
    release = threading.Event()
    seen = []

    def fake_guarded(run_config):
        seen.append(run_config)
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(pipeline, "_run_pipeline_guarded", fake_guarded)
    assert pipeline.start_pipeline(caller_config) is True
    assert started.wait(timeout=1)
    return seen, release


def _cleanup_pipeline_thread(release: threading.Event | None = None) -> None:
    if release is not None:
        release.set()
    with pipeline._pipeline_start_lock:
        thread = pipeline._pipeline_thread
    if thread is not None:
        thread.join(timeout=2)
    with pipeline._pipeline_start_lock:
        pipeline._pipeline_thread = None


@pytest.mark.parametrize(
    ("persisted_enabled", "caller_config", "expected_enabled"),
    [
        (False, {"auto_offer": {"enabled": True}, "pipeline": {}}, False),
        (True, {"auto_offer": {"enabled": False}, "pipeline": {}}, True),
        (True, {"pipeline": {}}, True),
    ],
)
def test_start_uses_persisted_auto_offer_flag_not_request_body(
    monkeypatch,
    persisted_enabled,
    caller_config,
    expected_enabled,
):
    release = None
    try:
        seen, release = _start_and_capture(
            monkeypatch,
            caller_config,
            persisted_enabled=persisted_enabled,
        )
        assert seen[0]["auto_offer"]["enabled"] is expected_enabled
    finally:
        _cleanup_pipeline_thread(release)


def test_persisted_false_with_absent_section_preserves_historical_raw_shape(monkeypatch):
    release = None
    try:
        caller = {"run": 1}
        seen, release = _start_and_capture(
            monkeypatch,
            caller,
            persisted_enabled=False,
        )
        assert seen == [{"run": 1}]
    finally:
        _cleanup_pipeline_thread(release)


def test_start_detaches_run_snapshot_from_later_caller_mutation(monkeypatch):
    release = None
    try:
        caller = {
            "auto_offer": {"enabled": True},
            "pipeline": {"exclude_keywords": ["before"]},
        }
        seen, release = _start_and_capture(
            monkeypatch,
            caller,
            persisted_enabled=False,
        )
        caller["auto_offer"]["enabled"] = True
        caller["pipeline"]["exclude_keywords"].append("after")

        assert seen[0]["auto_offer"]["enabled"] is False
        assert seen[0]["pipeline"]["exclude_keywords"] == ["before"]
    finally:
        _cleanup_pipeline_thread(release)


class _AlivePipelineThread:
    def is_alive(self) -> bool:
        return True


def _install_config_state(monkeypatch, *, enabled: bool, running: bool):
    from app.auto_offer.runtime_mode import AutoOfferRuntimeMode, AutoOfferRuntimeState

    state = {
        "auto_offer": {"enabled": enabled},
        "pipeline": {"max_discount": 0.8},
    }
    updates = []

    def load():
        return {
            "auto_offer": dict(state["auto_offer"]),
            "pipeline": dict(state["pipeline"]),
        }

    def update(patch):
        updates.append(patch)
        auto_offer = patch.get("auto_offer") if isinstance(patch, dict) else None
        if isinstance(auto_offer, dict) and "enabled" in auto_offer:
            state["auto_offer"]["enabled"] = bool(auto_offer["enabled"])
        pipeline_patch = patch.get("pipeline") if isinstance(patch, dict) else None
        if isinstance(pipeline_patch, dict):
            state["pipeline"].update(pipeline_patch)
        return load()

    monkeypatch.setattr(pipeline, "load_app_config_validated", load)
    monkeypatch.setattr(pipeline, "update_app_config_validated", update)
    monkeypatch.setattr(pipeline, "_shutdown_pending", False)
    monkeypatch.setattr(pipeline, "_pipeline_maintenance_reason", "")
    monkeypatch.setattr(
        pipeline,
        "preflight_auto_offer_enable",
        lambda **_kwargs: AutoOfferRuntimeState(
            requested_enabled=True,
            active_delivery_count=0,
            mode=AutoOfferRuntimeMode.ON,
        ),
    )
    with pipeline._pipeline_start_lock:
        pipeline._pipeline_thread = _AlivePipelineThread() if running else None
    return state, updates


@pytest.mark.parametrize(("current", "requested"), [(False, True), (True, False)])
def test_actual_toggle_is_blocked_while_pipeline_running_without_persisting(
    monkeypatch, current, requested
):
    state, updates = _install_config_state(
        monkeypatch,
        enabled=current,
        running=True,
    )

    with pytest.raises(pipeline.PipelineMaintenanceBlocked):
        pipeline.update_auto_offer_enabled_config(
            {"auto_offer": {"enabled": requested}}
        )

    assert state["auto_offer"]["enabled"] is current
    assert updates == []


def test_same_value_full_config_save_is_allowed_while_running(monkeypatch):
    state, updates = _install_config_state(
        monkeypatch,
        enabled=True,
        running=True,
    )

    result = pipeline.update_auto_offer_enabled_config(
        {
            "auto_offer": {"enabled": True},
            "pipeline": {"max_discount": 0.7},
        }
    )

    assert result["auto_offer"]["enabled"] is True
    assert state["pipeline"]["max_discount"] == 0.7
    assert len(updates) == 1


def test_actual_toggle_succeeds_when_pipeline_is_stopped(monkeypatch):
    state, updates = _install_config_state(
        monkeypatch,
        enabled=False,
        running=False,
    )

    result = pipeline.update_auto_offer_enabled_config(
        {"auto_offer": {"enabled": True}}
    )

    assert result["auto_offer"]["enabled"] is True
    assert state["auto_offer"]["enabled"] is True
    assert len(updates) == 1


def test_enable_runs_existing_store_maintenance_before_read_only_preflight(monkeypatch):
    from app.auto_offer.runtime_mode import AutoOfferRuntimeMode, AutoOfferRuntimeState

    state, updates = _install_config_state(
        monkeypatch,
        enabled=False,
        running=False,
    )
    calls = []

    monkeypatch.setattr(
        pipeline,
        "maintain_existing_store_for_enable",
        lambda: calls.append("maintenance"),
    )
    monkeypatch.setattr(
        pipeline,
        "preflight_auto_offer_enable",
        lambda **_kwargs: calls.append("preflight")
        or AutoOfferRuntimeState(
            requested_enabled=True,
            active_delivery_count=0,
            mode=AutoOfferRuntimeMode.ON,
        ),
    )

    pipeline.update_auto_offer_enabled_config({"auto_offer": {"enabled": True}})

    assert calls == ["maintenance", "preflight"]
    assert state["auto_offer"]["enabled"] is True
    assert len(updates) == 1


def test_actual_toggle_is_blocked_during_shutdown(monkeypatch):
    state, updates = _install_config_state(
        monkeypatch,
        enabled=False,
        running=False,
    )
    monkeypatch.setattr(pipeline, "_shutdown_pending", True)

    with pytest.raises(pipeline.PipelineMaintenanceBlocked):
        pipeline.update_auto_offer_enabled_config(
            {"auto_offer": {"enabled": True}}
        )

    assert state["auto_offer"]["enabled"] is False
    assert updates == []


def test_unrelated_config_route_keeps_historical_direct_update_path(monkeypatch):
    calls = []

    def direct_update(patch):
        calls.append(("direct", patch))
        return {"pipeline": {"max_discount": 0.7}}

    monkeypatch.setattr(config_route, "update_app_config_validated", direct_update)
    monkeypatch.setattr(
        pipeline,
        "update_auto_offer_enabled_config",
        lambda _patch: (_ for _ in ()).throw(
            AssertionError("unrelated save inspected Auto Offer lifecycle")
        ),
    )

    result = config_route.api_save_config(
        config_route.ConfigBody(config={"pipeline": {"max_discount": 0.7}})
    )

    assert result["ok"] is True
    assert calls == [("direct", {"pipeline": {"max_discount": 0.7}})]


def test_config_route_returns_stable_failure_for_blocked_toggle(monkeypatch):
    def blocked(_patch):
        raise pipeline.PipelineMaintenanceBlocked("pipeline running")

    monkeypatch.setattr(pipeline, "update_auto_offer_enabled_config", blocked)
    monkeypatch.setattr(
        config_route,
        "update_app_config_validated",
        lambda _patch: (_ for _ in ()).throw(
            AssertionError("toggle bypassed lifecycle helper")
        ),
    )

    result = config_route.api_save_config(
        config_route.ConfigBody(config={"auto_offer": {"enabled": True}})
    )

    assert result == {
        "ok": False,
        "code": "AUTO_OFFER_CONFIG_CHANGE_BLOCKED",
        "error": "pipeline running",
    }


def test_pipeline_runtime_blocker_always_uses_effective_runtime_facade(monkeypatch):
    from app.auto_offer.runtime_mode import AutoOfferRuntimeMode, AutoOfferRuntimeState

    config = {"auto_offer": {"enabled": False}, "pipeline": {}}
    calls = []
    blocked = AutoOfferRuntimeState(
        requested_enabled=False,
        active_delivery_count=0,
        mode=AutoOfferRuntimeMode.BLOCKED,
        reason="duplicate_host_identity",
    )
    monkeypatch.setattr(pipeline, "load_app_config_validated", lambda: config)
    monkeypatch.setattr(pipeline, "_lifecycle_host_purchases", lambda: [])
    monkeypatch.setattr(
        pipeline,
        "get_effective_runtime_state",
        lambda *, config, purchases: calls.append((config, purchases)) or blocked,
    )

    blocker = pipeline.get_pipeline_runtime_blocker()

    assert blocker["code"] == "AUTO_OFFER_RUNTIME_BLOCKED"
    assert blocker["mode"] == AutoOfferRuntimeMode.BLOCKED.value
    assert blocker["message"] == "duplicate_host_identity"
    assert calls == [(config, [])]


def test_start_pipeline_rejects_blocked_effective_runtime_without_store(monkeypatch):
    from contextlib import nullcontext
    from app.auto_offer.runtime_mode import AutoOfferRuntimeMode, AutoOfferRuntimeState
    from app.services import buff_auth

    blocked = AutoOfferRuntimeState(
        requested_enabled=False,
        active_delivery_count=0,
        mode=AutoOfferRuntimeMode.BLOCKED,
        reason="duplicate_host_identity",
    )
    state = _State()
    monkeypatch.setattr(buff_auth, "get_buff_auth_lock", lambda: threading.RLock())
    monkeypatch.setattr(pipeline, "buff_activity_guard", nullcontext)
    monkeypatch.setattr(pipeline, "get_unresolved_checkout", lambda: None)
    monkeypatch.setattr(pipeline, "get_pipeline_start_blocker", lambda: {})
    monkeypatch.setattr(pipeline, "get_state", lambda: state)
    monkeypatch.setattr(pipeline, "load_app_config_validated", lambda: {"auto_offer": {"enabled": False}})
    monkeypatch.setattr(pipeline, "_lifecycle_host_purchases", lambda: [])
    monkeypatch.setattr(pipeline, "get_effective_runtime_state", lambda **_kwargs: blocked)
    monkeypatch.setattr(
        pipeline,
        "_run_pipeline_guarded",
        lambda *_args: (_ for _ in ()).throw(AssertionError("blocked pipeline started")),
    )
    monkeypatch.setattr(pipeline, "_shutdown_pending", False)
    monkeypatch.setattr(pipeline, "_pipeline_maintenance_reason", "")
    with pipeline._pipeline_start_lock:
        pipeline._pipeline_thread = None

    assert pipeline.start_pipeline({"pipeline": {}}) is False
    with pipeline._pipeline_start_lock:
        assert pipeline._pipeline_thread is None


def test_start_and_toggle_serialize_on_same_lifecycle_lock(monkeypatch):
    from app.services import buff_auth

    persisted = {"enabled": False}
    updates = []
    start_inside_load = threading.Event()
    allow_start_to_continue = threading.Event()
    toggle_entered = threading.Event()
    worker_started = threading.Event()
    worker_release = threading.Event()
    seen_run_configs = []

    monkeypatch.setattr(buff_auth, "get_buff_auth_lock", lambda: threading.RLock())
    monkeypatch.setattr(pipeline, "buff_activity_guard", _activity_guard)
    monkeypatch.setattr(pipeline, "get_unresolved_checkout", lambda: None)
    monkeypatch.setattr(pipeline, "get_pipeline_start_blocker", lambda: {})
    monkeypatch.setattr(pipeline, "get_state", lambda: _State())
    monkeypatch.setattr(pipeline, "_shutdown_pending", False)
    monkeypatch.setattr(pipeline, "_pipeline_maintenance_reason", "")
    with pipeline._pipeline_start_lock:
        pipeline._pipeline_thread = None

    start_caller_ident = {"value": None}

    def load():
        if threading.get_ident() == start_caller_ident["value"]:
            start_inside_load.set()
            assert allow_start_to_continue.wait(timeout=2)
        return {"auto_offer": {"enabled": persisted["enabled"]}}

    def update(patch):
        updates.append(patch)
        persisted["enabled"] = patch["auto_offer"]["enabled"]
        return {"auto_offer": {"enabled": persisted["enabled"]}}

    def fake_guarded(run_config):
        seen_run_configs.append(run_config)
        worker_started.set()
        worker_release.wait(timeout=5)

    monkeypatch.setattr(pipeline, "load_app_config_validated", load)
    monkeypatch.setattr(pipeline, "update_app_config_validated", update)
    monkeypatch.setattr(pipeline, "_run_pipeline_guarded", fake_guarded)

    start_result = {}
    toggle_result = {}

    def call_start():
        start_caller_ident["value"] = threading.get_ident()
        start_result["value"] = pipeline.start_pipeline(
            {"auto_offer": {"enabled": True}}
        )

    def call_toggle():
        toggle_entered.set()
        try:
            pipeline.update_auto_offer_enabled_config(
                {"auto_offer": {"enabled": True}}
            )
        except Exception as exc:  # exact type asserted below
            toggle_result["error"] = exc

    start_thread = threading.Thread(target=call_start, name="task027-start-call")
    toggle_thread = threading.Thread(target=call_toggle, name="task027-toggle-call")
    start_thread.start()
    assert start_inside_load.wait(timeout=1)
    toggle_thread.start()
    assert toggle_entered.wait(timeout=1)
    toggle_thread.join(timeout=0.05)
    assert toggle_thread.is_alive()

    allow_start_to_continue.set()
    start_thread.join(timeout=2)
    assert start_result["value"] is True
    assert worker_started.wait(timeout=1)
    toggle_thread.join(timeout=2)

    assert isinstance(toggle_result.get("error"), pipeline.PipelineMaintenanceBlocked)
    assert persisted["enabled"] is False
    assert updates == []
    assert seen_run_configs[0]["auto_offer"]["enabled"] is False

    worker_release.set()
    _cleanup_pipeline_thread()
