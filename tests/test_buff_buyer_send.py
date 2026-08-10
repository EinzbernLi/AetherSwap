import base64

import pytest
import requests
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import unpad
from requests.cookies import RequestsCookieJar

from buff.buyer import BuffBuyer
from buff.buyer_send import (
    API_BUYER_SEND_OFFER,
    BuffBuyerSendError,
    BuffBuyerSendTransport,
    encrypt_buyer_info,
)
from buff.request_policy import (
    BuffRequestPolicy,
    BuffWriteResultUnknown,
)

STEAM_ID = "76561198000000000"
OTHER_STEAM_ID = "76561198000000001"
ORDER_ID = "123456789"
STEAM_COOKIE = (
    "sessionid=session-secret; "
    f"steamLoginSecure={STEAM_ID}%7C%7Clogin-secret"
)


class FakeResponse:
    def __init__(
        self,
        data,
        *,
        status_code=200,
        headers=None,
        raw_text=None,
        url=API_BUYER_SEND_OFFER,
        history=None,
    ):
        self._data = data
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "application/json"}
        self.text = (
            raw_text
            if raw_text is not None
            else ("" if data is None else __import__("json").dumps(data))
        )
        self.url = url
        self.history = history or []
        self.cookies = RequestsCookieJar()

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.cookies = RequestsCookieJar()
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self):
        pass


def no_wait_policy():
    return BuffRequestPolicy(min_interval=0, state_path=None, persist=False)


@pytest.fixture(scope="module")
def rsa_key():
    return RSA.generate(2048)


def public_key_b64(key):
    return base64.b64encode(key.public_key().export_key(format="DER")).decode("ascii")


def decrypt_envelope(encoded, key):
    raw = base64.b64decode(encoded, validate=True)
    rsa_size = key.size_in_bytes()
    encrypted_key = raw[:rsa_size]
    iv = raw[rsa_size : rsa_size + 16]
    ciphertext = raw[rsa_size + 16 :]
    sentinel = object()
    aes_key = PKCS1_v1_5.new(key).decrypt(encrypted_key, sentinel)
    assert aes_key is not sentinel
    plaintext = AES.new(aes_key, AES.MODE_CBC, iv).decrypt(ciphertext)
    return unpad(plaintext, AES.block_size).decode("utf-8")


def test_crypto_round_trip_matches_frozen_envelope(rsa_key):
    encrypted = encrypt_buyer_info(
        STEAM_COOKIE,
        expected_steam_id=STEAM_ID,
        public_key_b64=public_key_b64(rsa_key),
    )
    assert decrypt_envelope(encrypted, rsa_key) == STEAM_COOKIE


def test_crypto_uses_fresh_random_key_and_iv(rsa_key):
    key_b64 = public_key_b64(rsa_key)
    first = encrypt_buyer_info(
        STEAM_COOKIE,
        expected_steam_id=STEAM_ID,
        public_key_b64=key_b64,
    )
    second = encrypt_buyer_info(
        STEAM_COOKIE,
        expected_steam_id=STEAM_ID,
        public_key_b64=key_b64,
    )

    assert first != second
    assert decrypt_envelope(first, rsa_key) == STEAM_COOKIE
    assert decrypt_envelope(second, rsa_key) == STEAM_COOKIE


@pytest.mark.parametrize(
    "public_key_b64",
    ["", "not-base64", base64.b64encode(b"not-rsa").decode("ascii")],
)
def test_invalid_public_key_fails_locally(rsa_key, public_key_b64):
    with pytest.raises(BuffBuyerSendError, match="buyer_info_public_key_invalid"):
        encrypt_buyer_info(
            STEAM_COOKIE,
            expected_steam_id=STEAM_ID,
            public_key_b64=public_key_b64,
        )


def test_raw_secure_separator_is_accepted_and_identity_bound(rsa_key):
    raw_cookie = f"steamLoginSecure={STEAM_ID}||token"
    encrypted = encrypt_buyer_info(
        raw_cookie,
        expected_steam_id=STEAM_ID,
        public_key_b64=public_key_b64(rsa_key),
    )
    assert decrypt_envelope(encrypted, rsa_key) == raw_cookie


@pytest.mark.parametrize(
    "cookie",
    [
        "",
        "sessionid=x",
        "steamLoginSecure=",
        "steamLoginSecure=   ",
        f"steamLoginSecure={STEAM_ID}",
        f"steamLoginSecure={STEAM_ID}%7C%7C",
        f"steamLoginSecure={STEAM_ID}||token%7C%7Cother",
        f"steamLoginSecure={STEAM_ID}%7C%7Ctoken; steamLoginSecure={STEAM_ID}%7C%7Cother",
        "broken-cookie",
    ],
)
def test_invalid_steam_cookie_fails_before_http(cookie):
    session = FakeSession(FakeResponse({"code": "OK"}))
    buyer = BuffBuyer(
        "session=buff; csrf_token=csrf",
        session=session,
        request_policy=no_wait_policy(),
        steam_id=STEAM_ID,
    )
    transport = BuffBuyerSendTransport(buyer)

    with pytest.raises(BuffBuyerSendError):
        transport.send(
            steam_cookie_string=cookie,
            buff_order_id=ORDER_ID,
            steam_id=STEAM_ID,
            timeout_seconds=5,
        )

    assert session.calls == []


def test_steam_cookie_embedded_identity_mismatch_fails_before_http():
    session = FakeSession(FakeResponse({"code": "OK"}))
    buyer = BuffBuyer(
        "session=buff; csrf_token=csrf",
        session=session,
        request_policy=no_wait_policy(),
        steam_id=STEAM_ID,
    )
    transport = BuffBuyerSendTransport(buyer)
    wrong_cookie = f"steamLoginSecure={OTHER_STEAM_ID}%7C%7Cwrong-secret"

    with pytest.raises(BuffBuyerSendError, match="steam_cookie_identity_mismatch"):
        transport.send(
            steam_cookie_string=wrong_cookie,
            buff_order_id=ORDER_ID,
            steam_id=STEAM_ID,
            timeout_seconds=5,
        )

    assert session.calls == []


@pytest.mark.parametrize(
    ("order_id", "steam_id", "timeout"),
    [
        ("", STEAM_ID, 5),
        (" 1", STEAM_ID, 5),
        (ORDER_ID, "", 5),
        (ORDER_ID, "001", 5),
        (ORDER_ID, "abc", 5),
        (ORDER_ID, STEAM_ID, 0),
        (ORDER_ID, STEAM_ID, float("inf")),
    ],
)
def test_invalid_identity_or_timeout_fails_before_http(order_id, steam_id, timeout):
    session = FakeSession(FakeResponse({"code": "OK"}))
    buyer = BuffBuyer(
        "session=buff; csrf_token=csrf",
        session=session,
        request_policy=no_wait_policy(),
        steam_id=STEAM_ID,
    )
    transport = BuffBuyerSendTransport(buyer)

    with pytest.raises(BuffBuyerSendError):
        transport.send(
            steam_cookie_string=STEAM_COOKIE,
            buff_order_id=order_id,
            steam_id=steam_id,
            timeout_seconds=timeout,
        )

    assert session.calls == []


def test_bound_buff_steam_identity_mismatch_fails_before_http():
    session = FakeSession(FakeResponse({"code": "OK"}))
    buyer = BuffBuyer(
        "session=buff; csrf_token=csrf",
        session=session,
        request_policy=no_wait_policy(),
        steam_id=OTHER_STEAM_ID,
    )
    transport = BuffBuyerSendTransport(buyer)

    with pytest.raises(BuffBuyerSendError, match="steam_identity_mismatch"):
        transport.send(
            steam_cookie_string=STEAM_COOKIE,
            buff_order_id=ORDER_ID,
            steam_id=STEAM_ID,
            timeout_seconds=5,
        )

    assert session.calls == []


def test_exact_single_order_post_uses_existing_buff_request_policy():
    session = FakeSession(FakeResponse({"code": "OK", "data": {}}))
    buyer = BuffBuyer(
        "session=buff; csrf_token=csrf-secret",
        session=session,
        request_policy=no_wait_policy(),
        steam_id=STEAM_ID,
    )
    transport = BuffBuyerSendTransport(buyer)

    result = transport.send(
        steam_cookie_string=STEAM_COOKIE,
        buff_order_id=ORDER_ID,
        steam_id=STEAM_ID,
        timeout_seconds=7,
    )

    assert result == {"code": "OK", "data": {}}
    assert len(session.calls) == 1
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url == API_BUYER_SEND_OFFER
    assert kwargs["timeout"] == 7.0
    assert set(kwargs["json"]) == {"buyer_info", "bill_orders", "steamid"}
    assert kwargs["json"]["bill_orders"] == [ORDER_ID]
    assert kwargs["json"]["steamid"] == STEAM_ID
    assert kwargs["json"]["buyer_info"] != STEAM_COOKIE
    assert "session-secret" not in kwargs["json"]["buyer_info"]
    assert "login-secret" not in kwargs["json"]["buyer_info"]
    assert kwargs["headers"]["X-Csrftoken"] == "csrf-secret"
    assert kwargs["headers"]["Origin"] == "https://buff.163.com"
    assert kwargs["headers"]["Referer"].startswith("https://buff.163.com/")


def test_network_exception_is_unknown_and_never_retried():
    session = FakeSession(requests.Timeout("secret must not matter"))
    buyer = BuffBuyer(
        "session=buff; csrf_token=csrf",
        session=session,
        request_policy=no_wait_policy(),
        steam_id=STEAM_ID,
    )
    transport = BuffBuyerSendTransport(buyer)

    with pytest.raises(BuffWriteResultUnknown) as caught:
        transport.send(
            steam_cookie_string=STEAM_COOKIE,
            buff_order_id=ORDER_ID,
            steam_id=STEAM_ID,
            timeout_seconds=5,
        )

    assert len(session.calls) == 1
    assert "login-secret" not in str(caught.value)
    assert "session-secret" not in str(caught.value)


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse({"code": "ERROR"}, status_code=500),
        FakeResponse(
            {"code": "TOO_MANY_REQUESTS"},
            status_code=429,
            headers={"Content-Type": "application/json", "Retry-After": "60"},
        ),
        FakeResponse(
            None,
            status_code=200,
            headers={"Content-Type": "text/html"},
            raw_text="<html>challenge</html>",
        ),
        FakeResponse(
            None,
            status_code=200,
            headers={"Content-Type": "application/json"},
            raw_text="{broken",
        ),
    ],
)
def test_ambiguous_write_responses_are_unknown_and_not_retried(response):
    session = FakeSession(response)
    buyer = BuffBuyer(
        "session=buff; csrf_token=csrf",
        session=session,
        request_policy=no_wait_policy(),
        steam_id=STEAM_ID,
    )
    transport = BuffBuyerSendTransport(buyer)

    with pytest.raises(BuffWriteResultUnknown):
        transport.send(
            steam_cookie_string=STEAM_COOKIE,
            buff_order_id=ORDER_ID,
            steam_id=STEAM_ID,
            timeout_seconds=5,
        )

    assert len(session.calls) == 1


def test_missing_buff_csrf_fails_before_http_and_does_not_leak_cookie():
    session = FakeSession(FakeResponse({"code": "OK"}))
    buyer = BuffBuyer(
        "session=buff",
        session=session,
        request_policy=no_wait_policy(),
        steam_id=STEAM_ID,
    )
    transport = BuffBuyerSendTransport(buyer)

    with pytest.raises(Exception) as caught:
        transport.send(
            steam_cookie_string=STEAM_COOKIE,
            buff_order_id=ORDER_ID,
            steam_id=STEAM_ID,
            timeout_seconds=5,
        )

    assert session.calls == []
    assert "login-secret" not in str(caught.value)
    assert "session-secret" not in str(caught.value)
