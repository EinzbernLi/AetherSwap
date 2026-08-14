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
    assert "autoOfferIntentDirty" in settings
    assert "autoOfferPatch" in settings
    assert "enabled: !!autoOfferCheckbox.checked" in settings
    assert 'typeof autoOfferIntentBaseline !== "boolean"' in settings
    assert "checkbox.checked !== autoOfferIntentBaseline" in settings
    assert "autoOfferIntentBaseline = gAutoOffer.checked" in settings
    assert "autoOfferIntentDirty = false" in settings
    assert "autoOfferIntentDirty && autoOfferCheckbox" in settings
    listener_start = settings.index("function bindAutoOfferIntentTracking")
    listener_end = settings.index("function detectBrowserTimezone", listener_start)
    listener_source = settings[listener_start:listener_end]
    assert "autoOfferIntentDirty = true" not in listener_source
    assert "auto_offer_runtime" not in settings


def test_runtime_display_uses_status_payload_and_all_required_modes():
    main = _source("web/js/main.js")

    assert main.count('API + "/status"') == 1
    assert "status.auto_offer_runtime" in main
    for mode, label in {
        "off": "关闭",
        "enabling": "开启中",
        "on": "开启",
        "draining": "排空中",
        "blocked": "阻止",
    }.items():
        assert f'{mode}: "{label}"' in main
    assert "runtime.reason === null || typeof runtime.reason === \"string\"" in main
    assert 'OFF: "关闭"' not in main
    assert 'ON: "开启"' not in main
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


def test_d3_runtime_source_has_no_history_or_authority_dependencies():
    main = _source("web/js/main.js")

    render_start = main.index("function renderAutoOfferRuntime")
    render_end = main.index("let reloginType", render_start)
    render_source = main[render_start:render_end]
    assert "fetch(" not in render_source
    assert "localStorage" not in render_source
    assert "auto_offer.db" not in render_source
