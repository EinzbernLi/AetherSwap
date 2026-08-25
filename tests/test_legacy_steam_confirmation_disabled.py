import ast
import inspect
import re


def _messages(ctx):
    return [call.args[0] for call in ctx.log.call_args_list]


def test_enabled_legacy_confirmation_logs_stable_warning_without_sleep_or_network(
    monkeypatch,
):
    from unittest.mock import MagicMock

    from app import sell_pipeline

    ctx = MagicMock()
    cfg = {
        "steam_confirm": {
            "enabled": True,
            "identity_secret": "secret-must-not-be-read",
            "device_id": "device-must-not-be-read",
        }
    }
    monkeypatch.setattr(
        sell_pipeline,
        "jittered_sleep",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled legacy confirmation must not sleep")
        ),
    )

    sell_pipeline._auto_confirm_listings(
        ctx,
        cfg,
    )

    assert _messages(ctx) == [
        "[确认] legacy bulk confirmation disabled; skipping"
    ]
    assert ctx.log.call_args.args[1] == "warn"
    assert "secret" not in repr(ctx.log.call_args)


def test_disabled_legacy_confirmation_is_silent(monkeypatch):
    from unittest.mock import MagicMock

    from app import sell_pipeline

    ctx = MagicMock()
    monkeypatch.setattr(
        sell_pipeline,
        "jittered_sleep",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled legacy confirmation must not sleep")
        ),
    )

    sell_pipeline._auto_confirm_listings(
        ctx,
        {"steam_confirm": {"enabled": False}},
    )

    ctx.log.assert_not_called()


def test_legacy_compatibility_entry_is_fixed_fail_closed_and_side_effect_free(
    monkeypatch,
):
    import app.steam_confirm as steam_confirm

    class ForbiddenSession:
        def __init__(self, *args, **kwargs):
            raise AssertionError("legacy compatibility entry must not create a session")

    monkeypatch.setattr(
        steam_confirm,
        "requests",
        type("Requests", (), {"Session": ForbiddenSession}),
        raising=False,
    )

    result = steam_confirm.auto_confirm_once(
        identity_secret="secret",
        device_id="device",
        steam_id="steam-id",
        cookies="cookie=secret",
    )

    assert result == (False, 0, "legacy_bulk_confirmation_disabled")


def test_legacy_bulk_and_sell_pipeline_dependency_markers_are_absent():
    import app.sell_pipeline as sell_pipeline
    import app.steam_confirm as steam_confirm

    sell_source = inspect.getsource(sell_pipeline)
    confirm_source = inspect.getsource(steam_confirm)
    for marker in (
        "from app.steam_confirm",
        "auto_confirm_once(",
    ):
        assert marker not in sell_source

    for marker in (
        "accept_all",
        "getlist",
        "multiajaxop",
        '"legacy_bulk_confirm"',
        "urllib3.disable_warnings",
    ):
        assert marker not in confirm_source

    normalized_confirm_source = re.sub(r"\s+", "", confirm_source)
    for marker in (
        "verify=False",
        "importrequests",
        "fromrequests",
        "importurllib3",
        "requests.Session",
        "urllib3",
    ):
        assert marker not in normalized_confirm_source


def test_disabled_hook_has_no_credential_signature_or_call_flow():
    import app.sell_pipeline as sell_pipeline

    tree = ast.parse(inspect.getsource(sell_pipeline))
    hook = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_auto_confirm_listings"
    )
    assert [arg.arg for arg in hook.args.args] == ["ctx", "cfg"]
    hook_source = ast.get_source_segment(inspect.getsource(sell_pipeline), hook)
    assert hook_source is not None
    for marker in (
        "steam_id",
        "cookies",
        "identity_secret",
        "device_id",
    ):
        assert marker not in hook_source

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_auto_confirm_listings"
    ]
    assert len(calls) == 1
    assert len(calls[0].args) == 2
    assert calls[0].keywords == []
    call_source = ast.get_source_segment(inspect.getsource(sell_pipeline), calls[0])
    assert call_source is not None
    for marker in (
        "steam_id",
        "cookies",
        "identity_secret",
        "device_id",
    ):
        assert marker not in call_source


def test_sell_pipeline_still_calls_disabled_hook_only_after_listing_success():
    import app.sell_pipeline as sell_pipeline

    tree = ast.parse(inspect.getsource(sell_pipeline))
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_submit_listings" in names
    source = inspect.getsource(sell_pipeline._run_sell_phase_impl)
    assert "if listed:" in source
    assert "_auto_confirm_listings(ctx, cfg" in source
