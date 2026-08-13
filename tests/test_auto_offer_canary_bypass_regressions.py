from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import app.auto_offer.canary_authority as authority_module
from app.auto_offer.canary_authority import (
    CanaryAuthority,
    CanaryPermit,
    CanaryWriteBlockedError,
    CanaryWriteTarget,
)
from app.auto_offer.contracts import AutoOfferResult, DeliveryMode, DeliverySnapshot, DeliveryStatus
from app.auto_offer.store import StoredDelivery

ACCOUNT_ID = "account-1"
STEAM_ID = "76561198000000007"
ORDER_ID = "buff-order-7"
PURCHASE_ID = f"buff:{ORDER_ID}"


def _permit(*, permit_id="permit-1", owner_nonce="owner-1", created_at=1.0):
    return CanaryPermit(
        permit_id=permit_id,
        owner_nonce=owner_nonce,
        host_db_id=7,
        buff_order_id=ORDER_ID,
        purchase_id=PURCHASE_ID,
        account_id=ACCOUNT_ID,
        recipient_steam_id=STEAM_ID,
        expected_host_order_ids=(ORDER_ID,),
        expected_store_present=False,
        expected_store_revision=None,
        expected_store_status=None,
        expected_store_tradeoffer_id=None,
        created_at=created_at,
    )


def _host_row(order_id=ORDER_ID, db_id=7):
    return {
        "_db_id": db_id,
        "buff_order_id": order_id,
        "pending_receipt": True,
        "assetid": None,
    }


def _host_db(path):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE purchase ("
        "id INTEGER PRIMARY KEY, buff_order_id TEXT, pending_receipt INTEGER, assetid TEXT)"
    )
    connection.execute(
        "INSERT INTO purchase(id,buff_order_id,pending_receipt,assetid) VALUES(7,?,1,NULL)",
        (ORDER_ID,),
    )
    connection.commit()
    connection.close()
    return path


def _delivery(status, *, mode=None, revision=1):
    return StoredDelivery(
        DeliverySnapshot(
            purchase_id=PURCHASE_ID,
            buff_order_id=ORDER_ID,
            account_id=ACCOUNT_ID,
            recipient_steam_id=STEAM_ID,
            delivery_mode=mode,
            delivery_status=status,
            steam_tradeoffer_id=None,
            offer_attempted_at=None,
            offer_sent_at=None,
            received_at=None,
            delivery_error=None,
            pending_receipt=True,
            assetid=None,
        ),
        revision,
    )


def _install_test_authority(monkeypatch, tmp_path, *, stale=False):
    authority = CanaryAuthority(_root=tmp_path / "authority")
    owner_session = authority._arm_owner_session(_permit())
    if stale:
        owner_session.release_keep_fence()
        owner_session = None
    monkeypatch.setattr(authority_module, "_PRODUCTION_AUTHORITY", authority)
    return authority, owner_session


def test_stale_host_snapshot_second_row_before_send_is_stopped_by_live_db_barrier(monkeypatch, tmp_path):
    import app.auto_offer.host_integration as host_integration

    db_path = _host_db(tmp_path / "host.db")
    authority = CanaryAuthority(_root=tmp_path / "authority", _host_db_path=db_path)
    permit = _permit()
    owner_session = authority._arm_owner_session(permit)
    monkeypatch.setattr(authority_module, "_PRODUCTION_AUTHORITY", authority)
    monkeypatch.setattr(host_integration, "_exact_current_account", lambda: (ACCOUNT_ID, STEAM_ID))
    monkeypatch.setattr(host_integration, "_steam_cookie_for_expected", lambda _steam_id: "fake-cookie")

    fresh = _delivery(DeliveryStatus.PENDING_DIRECTION)
    adapter_calls = []

    class Bridge:
        account_id = ACCOUNT_ID
        recipient_steam_id = STEAM_ID

        def __init__(self):
            self.current = None

        def register_committed_purchase(self, _record):
            self.current = fresh
            return fresh

        def list_recoverable(self):
            return () if self.current is None else (self.current,)

        def get_by_purchase_id(self, purchase_id):
            return self.current if self.current and self.current.snapshot.purchase_id == purchase_id else None

        def step(self, delivery):
            if delivery.snapshot.delivery_status is DeliveryStatus.PENDING_DIRECTION:
                awaiting = StoredDelivery(
                    DeliverySnapshot(
                        purchase_id=PURCHASE_ID,
                        buff_order_id=ORDER_ID,
                        account_id=ACCOUNT_ID,
                        recipient_steam_id=STEAM_ID,
                        delivery_mode=DeliveryMode.BUYER_SENDS_OFFER,
                        delivery_status=DeliveryStatus.AWAITING_OFFER,
                        steam_tradeoffer_id=None,
                        offer_attempted_at=None,
                        offer_sent_at=None,
                        received_at=None,
                        delivery_error=None,
                        pending_receipt=True,
                        assetid=None,
                    ),
                    delivery.revision + 1,
                )
                self.current = awaiting
                connection = sqlite3.connect(db_path)
                connection.execute(
                    "INSERT INTO purchase(id,buff_order_id,pending_receipt,assetid) VALUES(8,'buff-order-8',1,NULL)"
                )
                connection.commit()
                connection.close()
                return SimpleNamespace(after=awaiting)
            assert delivery.snapshot.delivery_status is DeliveryStatus.AWAITING_OFFER
            target = CanaryWriteTarget(
                action="auto_offer_send",
                purchase_id=PURCHASE_ID,
                buff_order_id=ORDER_ID,
                account_id=ACCOUNT_ID,
                recipient_steam_id=STEAM_ID,
            )
            with owner_session.external_write_guard(target):
                adapter_calls.append("send")
            raise AssertionError("guard should have blocked")

        def close(self):
            pass

    integration = host_integration.HostAutoOfferIntegration(
        Bridge(),
        complete_purchase_receipt_by_id=lambda *_args: True,
        canary_permit=permit,
        canary_owner_session=owner_session,
    )
    assert integration.next_purchase_result([_host_row()]) is AutoOfferResult.BLOCKED
    assert adapter_calls == []
    owner_session.release_keep_fence()


@pytest.mark.parametrize("stale", [False, True])
def test_generic_host_transaction_mutations_are_fenced_before_db_calls(monkeypatch, tmp_path, stale):
    import app.state as state_module

    authority, owner_session = _install_test_authority(monkeypatch, tmp_path, stale=stale)
    calls = []
    monkeypatch.setattr(state_module, "db_append_purchase", lambda _value: calls.append("append_purchase"))
    monkeypatch.setattr(state_module, "db_append_sale", lambda _value: calls.append("append_sale"))
    monkeypatch.setattr(state_module, "db_clear_transactions", lambda: calls.append("clear"))
    monkeypatch.setattr(state_module, "db_replace_transactions", lambda *_args: calls.append("replace"))
    monkeypatch.setattr(state_module, "db_delete_purchase_by_id", lambda _db_id: calls.append("delete") or True)
    monkeypatch.setattr(state_module, "db_update_purchase_by_id", lambda *_args: calls.append("update") or True)

    state = state_module.State()
    operations = [
        lambda: state.append_purchase({}),
        lambda: state.append_sale({}),
        state.clear_transactions,
        lambda: state.replace_transactions([], []),
        lambda: state.delete_purchase_by_id(7),
        lambda: state.update_purchase_by_id(7, {}),
    ]
    for operation in operations:
        with pytest.raises(CanaryWriteBlockedError):
            operation()
    assert calls == []
    if owner_session is not None:
        owner_session.release_keep_fence()


def test_exact_host_receipt_can_only_reach_state_db_inside_owner_context(monkeypatch, tmp_path):
    import app.state as state_module

    authority = CanaryAuthority(_root=tmp_path / "authority")
    permit = _permit()
    owner_session = authority._arm_owner_session(permit)
    monkeypatch.setattr(authority_module, "_PRODUCTION_AUTHORITY", authority)
    calls = []
    monkeypatch.setattr(
        state_module,
        "db_complete_purchase_receipt_by_id",
        lambda db_id, order_id, assetid: calls.append((db_id, order_id, assetid)) or True,
    )
    state = state_module.State()

    with pytest.raises(CanaryWriteBlockedError):
        state.complete_purchase_receipt_by_id(7, ORDER_ID, "asset-7")
    assert calls == []

    outer = CanaryWriteTarget(
        action="host_receipt",
        purchase_id=PURCHASE_ID,
        buff_order_id=ORDER_ID,
        account_id=ACCOUNT_ID,
        recipient_steam_id=STEAM_ID,
        host_db_id=7,
        assetid="asset-7",
    )
    with owner_session.external_write_guard(outer):
        assert state.complete_purchase_receipt_by_id(7, ORDER_ID, "asset-7") is True
    assert calls == [(7, ORDER_ID, "asset-7")]
    owner_session.release_keep_fence()


def test_alternate_host_integration_with_public_permit_cannot_mint_owner_session(monkeypatch, tmp_path):
    import app.auto_offer.host_integration as host_integration

    authority = CanaryAuthority(_root=tmp_path / "authority")
    permit = _permit()
    owner_session = authority._arm_owner_session(permit)
    monkeypatch.setattr(authority_module, "_PRODUCTION_AUTHORITY", authority)

    class Bridge:
        account_id = ACCOUNT_ID
        recipient_steam_id = STEAM_ID

    legitimate = host_integration.HostAutoOfferIntegration(
        Bridge(),
        complete_purchase_receipt_by_id=lambda *_args: True,
        canary_permit=permit,
        canary_owner_session=owner_session,
    )
    assert legitimate.is_canary is True

    with pytest.raises(host_integration.HostAutoOfferIntegrationError, match="canary_authority_not_owned"):
        host_integration.HostAutoOfferIntegration(
            Bridge(),
            complete_purchase_receipt_by_id=lambda *_args: True,
            canary_permit=permit,
        )
    with pytest.raises(host_integration.HostAutoOfferIntegrationError, match="canary_authority_not_owned"):
        host_integration.HostAutoOfferIntegration(
            Bridge(),
            complete_purchase_receipt_by_id=lambda *_args: True,
            canary_permit=permit,
            canary_owner_session=object(),
        )
    owner_session.release_keep_fence()


def test_public_host_activation_rejects_any_caller_selected_authority_before_bridge(monkeypatch, tmp_path):
    import app.auto_offer.host_integration as host_integration

    production = CanaryAuthority(_root=tmp_path / "production")
    permit = _permit()
    owner_session = production._arm_owner_session(permit)
    monkeypatch.setattr(authority_module, "_PRODUCTION_AUTHORITY", production)

    alternate_a = CanaryAuthority(_root=tmp_path / "alternate-a")
    alternate_b = CanaryAuthority(_root=tmp_path / "alternate-b")
    identity_calls = []
    bridge_calls = []
    monkeypatch.setattr(
        host_integration,
        "_exact_current_account",
        lambda: identity_calls.append("identity") or (ACCOUNT_ID, STEAM_ID),
    )
    monkeypatch.setattr(
        host_integration,
        "_build_active_host_auto_offer_bridge",
        lambda **kwargs: bridge_calls.append(kwargs) or (_ for _ in ()).throw(AssertionError("bridge must not build")),
    )

    for injected_authority, injected_permit in (
        (production, permit),
        (alternate_a, permit),
        (alternate_b, permit),
        (alternate_a, None),
    ):
        with pytest.raises(
            host_integration.HostAutoOfferIntegrationError,
            match="canary_authority_injection_forbidden",
        ):
            host_integration.build_host_auto_offer_integration(
                config={"auto_offer": {"enabled": True}},
                buff_client=object(),
                complete_purchase_receipt_by_id=lambda *_args: True,
                canary_permit=injected_permit,
                canary_authority=injected_authority,
            )

    class Bridge:
        account_id = ACCOUNT_ID
        recipient_steam_id = STEAM_ID

    for injected_authority in (production, alternate_a, alternate_b):
        with pytest.raises(
            host_integration.HostAutoOfferIntegrationError,
            match="canary_authority_injection_forbidden",
        ):
            host_integration.HostAutoOfferIntegration(
                Bridge(),
                complete_purchase_receipt_by_id=lambda *_args: True,
                canary_authority=injected_authority,
            )

    assert identity_calls == []
    assert bridge_calls == []
    assert alternate_a.owns_canary is False
    assert alternate_b.owns_canary is False
    owner_session.release_keep_fence()


class _FakeSession:
    def __init__(self):
        self.headers = {}
        self.cookies = {}
        self.verify = True
        self.post_calls = []

    def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        return SimpleNamespace(status_code=200, text="", json=lambda: {})


@pytest.mark.parametrize("stale", [False, True])
def test_steam_delist_post_is_zero_during_active_or_stale_canary(monkeypatch, tmp_path, stale):
    import app.steam_delist as steam_delist

    authority, owner_session = _install_test_authority(monkeypatch, tmp_path, stale=stale)
    session = _FakeSession()
    monkeypatch.setattr(
        steam_delist,
        "get_steam",
        lambda: {"cookies": "steamLoginSecure=x; sessionid=s", "steam_id": STEAM_ID},
    )
    monkeypatch.setattr(steam_delist.requests, "Session", lambda: session)
    monkeypatch.setattr(
        steam_delist,
        "_get_mylistings_api",
        lambda _session: {
            "asset-1": {
                "listingid": "listing-1",
                "classid": "class-1",
                "instanceid": "0",
                "appid": "730",
                "contextid": "2",
            }
        },
    )
    monkeypatch.setattr(
        steam_delist,
        "_get_assetids_by_class_instance",
        lambda *_args, **_kwargs: {"asset-1"},
    )

    ok, _new_asset, error = steam_delist.delist_item("asset-1", "item")
    assert ok is False
    assert error == "canary_write_fenced"
    assert session.post_calls == []
    if owner_session is not None:
        owner_session.release_keep_fence()


class _ProxyManager:
    @staticmethod
    def get_proxies_for_request(failed=False):
        return {}


@pytest.mark.parametrize("stale", [False, True])
def test_gift_cart_and_checkout_posts_are_zero_during_active_or_stale_canary(monkeypatch, tmp_path, stale):
    import app.gift_engine as gift_engine
    import app.state as state_module
    import utils.proxy_manager as proxy_manager

    authority, owner_session = _install_test_authority(monkeypatch, tmp_path, stale=stale)
    logs = []
    post_calls = []
    monkeypatch.setattr(proxy_manager, "get_proxy_manager", lambda: _ProxyManager())
    monkeypatch.setattr(state_module, "log", lambda msg, *_args, **_kwargs: logs.append(msg))
    monkeypatch.setattr(
        gift_engine.requests,
        "post",
        lambda *_args, **_kwargs: post_calls.append("post") or SimpleNamespace(headers={"X-Eresult": "1"}),
    )

    token = "ACCESS_TOKEN_SENTINEL_DO_NOT_LOG"
    assert gift_engine._grpc_request(token, "AddItemsToCart/v1", "payload") is False
    assert gift_engine._do_checkout(
        "steamLoginSecure=x; sessionid=s",
        "CN",
        giftee_account_id=123,
    ) is False
    assert post_calls == []
    assert all(token not in message for message in logs)
    if owner_session is not None:
        owner_session.release_keep_fence()


def test_gift_finalize_has_its_own_final_boundary_fence(monkeypatch):
    import app.gift_engine as gift_engine
    import app.state as state_module
    import utils.proxy_manager as proxy_manager

    guard_calls = []
    post_calls = []

    @contextmanager
    def guard(action):
        guard_calls.append(action)
        if len(guard_calls) == 2:
            raise CanaryWriteBlockedError("canary_write_fenced")
        yield

    class Response:
        @staticmethod
        def json():
            return {"success": 1, "transid": "tx-1"}

    monkeypatch.setattr(gift_engine, "external_write_guard", guard)
    monkeypatch.setattr(proxy_manager, "get_proxy_manager", lambda: _ProxyManager())
    monkeypatch.setattr(state_module, "log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        gift_engine.requests,
        "post",
        lambda *_args, **_kwargs: post_calls.append("post") or Response(),
    )

    assert gift_engine._do_checkout(
        "steamLoginSecure=x; sessionid=s",
        "CN",
        giftee_account_id=123,
    ) is False
    assert guard_calls == ["steam_gift_checkout", "steam_gift_checkout"]
    assert post_calls == ["post"]


def test_normal_no_canary_gift_cart_boundary_remains_callable(monkeypatch):
    import app.gift_engine as gift_engine
    import app.state as state_module
    import utils.proxy_manager as proxy_manager

    post_calls = []
    monkeypatch.setattr(proxy_manager, "get_proxy_manager", lambda: _ProxyManager())
    monkeypatch.setattr(state_module, "log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        gift_engine.requests,
        "post",
        lambda *_args, **_kwargs: post_calls.append("post") or SimpleNamespace(headers={"X-Eresult": "1"}),
    )
    assert gift_engine._grpc_request("token", "AddItemsToCart/v1", "payload") is True
    assert post_calls == ["post"]
