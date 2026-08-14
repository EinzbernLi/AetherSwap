from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_auto_offer_settings_has_one_intent_checkbox_and_serializes_only_intent():
    html = _source("web/index.html")
    settings = _source("web/js/settings.js")

    assert html.count('id="cfg-auto-offer-enabled"') == 1
    assert 'gAutoOffer.checked = ao.enabled === true' in settings
    assert "auto_offer:" in settings
    assert "enabled: !!el(\"cfg-auto-offer-enabled\")?.checked" in settings
    assert "auto_offer_runtime" not in settings


def test_runtime_display_uses_status_payload_and_all_required_modes():
    main = _source("web/js/main.js")

    assert main.count('API + "/status"') == 1
    assert "status.auto_offer_runtime" in main
    for mode, label in {
        "OFF": "关闭",
        "ENABLING": "开启中",
        "ON": "开启",
        "DRAINING": "排空中",
        "BLOCKED": "阻止",
    }.items():
        assert f'{mode}: "{label}"' in main
    assert "Number.isInteger(runtime.active_delivery_count)" in main
    assert "runtime.active_delivery_count >= 0" in main
    assert "reasonEl.textContent" in main
    render_start = main.index("function renderAutoOfferRuntime")
    render_end = main.index("let reloginType", render_start)
    assert "innerHTML" not in main[render_start:render_end]


def test_missing_runtime_is_explicitly_unavailable_and_no_second_poller():
    main = _source("web/js/main.js")

    assert 'modeEl.textContent = "运行态：不可用"' in main
    assert 'reasonEl.textContent = "无法取得后端运行态证据"' in main
    assert main.count("setInterval(refreshStatus") == 1


def test_d3_production_changes_stay_within_allowlist():
    import subprocess

    result = subprocess.run(
        ["git", "diff", "--name-only", "bb79aac2c2a74289d4830b5e30162427f6157771"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    changed = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    assert changed <= {
        "web/index.html",
        "web/js/settings.js",
        "web/js/main.js",
        "tests/test_task042_ui_runtime_state.py",
    }
