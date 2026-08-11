import base64
import json

import pytest
import requests

from app.auto_offer.adapters import (
    PlatformAdapterProtocolError,
    PlatformAdapterTimeoutError,
)
from app.auto_offer.steam_confirmation_transport import (
    SteamConfirmationTransportError,
    SteamConfirmationWriteResultUnknown,
    SteamTradeOfferConfirmationTransport,
)


STEAM_ID = "76561198000000001"
OTHER_OFFER = "1234567890"
TARGET_OFFER = "9876543210"
IDENTITY_SECRET = base64.b64encode(b"0123456789abcdefghij").decode("ascii")
COOKIE = (
    "sessionid=session-token; steamCountry=TW; "
    f"steamLoginSecure={STEAM_ID}%7C%7Caccess-token"
)


class FakeResponse:
    def __init__(self, payload=None, *, status_code=200, raw_text=None):
        self.status_code = status_code
        if raw_text is None:
            raw_text = json.dumps(payload)
        self.text = raw_text
        self.content = raw_text.encode("utf-8")


class FakeSession:
    verify = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected extra HTTP call")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _list(*rows):
    return FakeResponse({"success": True, "conf": list(rows)})


def _detail(tradeoffer_id=None):
    if tradeoffer_id is None:
        html = "<div>confirmation without exact trade offer element</div>"
    else:
        html = f'<div id="tradeoffer_{tradeoffer_id}"></div>'
    return FakeResponse({"success": True, "html": html})


def _transport(session, **kwargs):
    return SteamTradeOfferConfirmationTransport(
        COOKIE,
        IDENTITY_SECRET,
        session=session,
        clock=lambda: 1_700_000_000,
        **kwargs,
    )


def _allow_calls(session):
    return [call for call in session.calls if call[0].endswith("/mobileconf/ajaxop")]


def test_exact_confirmation_performs_one_allow_after_unique_detail_match():
    session = FakeSession(
        [
            _list(
                {"id": "11", "nonce": "nonce-11", "creator_id": TARGET_OFFER},
                {"id": "22", "nonce": "nonce-22", "creator_id": OTHER_OFFER},
            ),
            _detail(TARGET_OFFER),
            _detail(OTHER_OFFER),
            FakeResponse({"success": True}),
        ]
    )

    result = _transport(session).confirm(TARGET_OFFER)

    assert result == {
        "steam_tradeoffer_id": TARGET_OFFER,
        "account_steam_id": STEAM_ID,
    }
    assert len(_allow_calls(session)) == 1
    allow_url, kwargs = _allow_calls(session)[0]
    assert allow_url == "https://steamcommunity.com/mobileconf/ajaxop"
    assert kwargs["params"]["op"] == "allow"
    assert kwargs["params"]["cid"] == "11"
    assert kwargs["params"]["ck"] == "nonce-11"
    assert kwargs["params"]["a"] == STEAM_ID
    assert kwargs["params"]["tag"] == "allow"
    assert kwargs["params"]["t"] == 1_700_000_000
    assert kwargs["params"]["p"].startswith("android:")
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == (5.0, 15.0)


def test_creator_id_alone_never_authorizes_mutation():
    session = FakeSession(
        [
            _list({"id": "11", "nonce": "nonce-11", "creator_id": TARGET_OFFER}),
            _detail(None),
        ]
    )

    with pytest.raises(
        SteamConfirmationTransportError,
        match="steam_confirmation_exact_offer_not_found",
    ):
        _transport(session).confirm(TARGET_OFFER)

    assert _allow_calls(session) == []


def test_suffix_match_is_never_accepted():
    session = FakeSession(
        [
            _list({"id": "11", "nonce": "nonce-11"}),
            _detail("123" + TARGET_OFFER),
        ]
    )

    with pytest.raises(SteamConfirmationTransportError):
        _transport(session).confirm(TARGET_OFFER)

    assert _allow_calls(session) == []


def test_ambiguous_exact_confirmations_block_before_mutation():
    session = FakeSession(
        [
            _list(
                {"id": "11", "nonce": "nonce-11"},
                {"id": "22", "nonce": "nonce-22"},
            ),
            _detail(TARGET_OFFER),
            _detail(TARGET_OFFER),
        ]
    )

    with pytest.raises(
        SteamConfirmationTransportError,
        match="steam_confirmation_exact_offer_ambiguous",
    ):
        _transport(session).confirm(TARGET_OFFER)

    assert _allow_calls(session) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"success": False, "conf": []},
        {"success": True, "conf": {}},
        {"success": True, "conf": [{"id": "", "nonce": "x"}]},
        {"success": True, "conf": [{"id": "1", "nonce": ""}]},
        {
            "success": True,
            "conf": [
                {"id": "1", "nonce": "x"},
                {"id": "1", "nonce": "y"},
            ],
        },
    ],
)
def test_malformed_or_unproven_list_never_mutates(payload):
    session = FakeSession([FakeResponse(payload)])

    with pytest.raises(SteamConfirmationTransportError):
        _transport(session).confirm(TARGET_OFFER)

    assert _allow_calls(session) == []


def test_unproven_detail_never_authorizes_mutation():
    session = FakeSession(
        [
            _list({"id": "11", "nonce": "nonce-11"}),
            FakeResponse(
                {
                    "success": False,
                    "html": f'<div id="tradeoffer_{TARGET_OFFER}"></div>',
                }
            ),
        ]
    )

    with pytest.raises(
        SteamConfirmationTransportError,
        match="steam_confirmation_detail_unproven",
    ):
        _transport(session).confirm(TARGET_OFFER)

    assert _allow_calls(session) == []


def test_list_limit_blocks_before_detail_or_mutation():
    rows = [
        {"id": str(index + 1), "nonce": f"nonce-{index + 1}"}
        for index in range(3)
    ]
    session = FakeSession([_list(*rows)])

    with pytest.raises(
        SteamConfirmationTransportError,
        match="steam_confirmation_list_too_large",
    ):
        _transport(session, max_confirmations=2).confirm(TARGET_OFFER)

    assert len(session.calls) == 1
    assert _allow_calls(session) == []


def test_preflight_timeout_is_distinct_and_never_mutates():
    session = FakeSession([requests.Timeout("secret should not escape")])

    with pytest.raises(PlatformAdapterTimeoutError, match="steam_confirmation_read_timeout"):
        _transport(session).confirm(TARGET_OFFER)

    assert _allow_calls(session) == []


@pytest.mark.parametrize(
    "write_response",
    [
        requests.Timeout("timeout"),
        RuntimeError("network"),
        FakeResponse({"success": False}),
        FakeResponse(raw_text="not-json"),
        FakeResponse({"success": True}, status_code=500),
    ],
)
def test_any_unproven_allow_result_is_result_unknown_and_never_retried(write_response):
    session = FakeSession(
        [
            _list({"id": "11", "nonce": "nonce-11"}),
            _detail(TARGET_OFFER),
            write_response,
        ]
    )

    with pytest.raises(
        SteamConfirmationWriteResultUnknown,
        match="steam_confirmation_write_result_unknown",
    ):
        _transport(session).confirm(TARGET_OFFER)

    assert len(_allow_calls(session)) == 1


@pytest.mark.parametrize(
    "identity_secret",
    [
        "",
        "not base64!",
        base64.b64encode(b"too-short").decode("ascii"),
        IDENTITY_SECRET + " ",
    ],
)
def test_invalid_identity_secret_is_rejected_before_network(identity_secret):
    session = FakeSession([])

    with pytest.raises(PlatformAdapterProtocolError, match="identity_secret is invalid"):
        SteamTradeOfferConfirmationTransport(
            COOKIE,
            identity_secret,
            session=session,
        )

    assert session.calls == []


def test_tls_verification_cannot_be_disabled():
    class InsecureSession(FakeSession):
        verify = False

    with pytest.raises(
        PlatformAdapterProtocolError,
        match="TLS verification must remain enabled",
    ):
        SteamTradeOfferConfirmationTransport(
            COOKIE,
            IDENTITY_SECRET,
            session=InsecureSession([]),
        )


@pytest.mark.parametrize(
    "offer_id",
    ["", "0", "01", " 1", "1 ", "abc"],
)
def test_tradeoffer_id_must_be_canonical_positive_decimal(offer_id):
    session = FakeSession([])

    with pytest.raises(PlatformAdapterProtocolError):
        _transport(session).confirm(offer_id)

    assert session.calls == []


def test_secret_never_appears_in_sanitized_failure_text_or_repr():
    session = FakeSession([RuntimeError(IDENTITY_SECRET)])
    transport = _transport(session)

    with pytest.raises(SteamConfirmationTransportError) as caught:
        transport.confirm(TARGET_OFFER)

    assert IDENTITY_SECRET not in str(caught.value)
    assert IDENTITY_SECRET not in repr(transport)


def test_source_has_no_retry_sleep_bulk_or_fuzzy_confirmation_path():
    import inspect
    import app.auto_offer.steam_confirmation_transport as module

    source = inspect.getsource(module)
    assert "time.sleep" not in source
    assert "multiajaxop" not in source
    assert "accept_all" not in source
    assert ".endswith(" not in source
    assert "match_end" not in source
