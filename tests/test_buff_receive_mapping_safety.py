import requests
import pytest
from types import SimpleNamespace

from app import receive_flow
from app.auto_offer.host_ownership import HostPurchaseOwnership


ITEM_NAME = "Danger Zone Case"


def _ownership_decisions(purchases, ownership):
    return [SimpleNamespace(ownership=ownership) for _ in purchases]


def _legacy_purchase(db_id=1, **changes):
    value = {
        "_db_id": db_id,
        "goods_id": 42,
        "name": ITEM_NAME,
        "at": float(db_id),
        "pending_receipt": True,
    }
    value.update(changes)
    return value


@pytest.mark.parametrize(
    "ownership",
    [
        HostPurchaseOwnership.MANAGED,
        HostPurchaseOwnership.RECEIPT_PENDING,
        HostPurchaseOwnership.RELEASED,
    ],
)
def test_protected_ownership_stops_legacy_receive_before_side_effects(
    monkeypatch,
    ownership,
):
    purchases = [_legacy_purchase()]
    calls = []

    monkeypatch.setattr(
        receive_flow,
        "classify_host_purchases",
        lambda rows: _ownership_decisions(rows, ownership),
    )

    received = receive_flow.try_receive_once(
        get_purchases=lambda: [dict(row) for row in purchases],
        update_purchase=lambda *_args, **_kwargs: calls.append("host-update") or True,
        get_buff_client=lambda: calls.append("buff-client") or object(),
        get_steam_credentials=lambda: calls.append("credentials") or {},
        scan_inventory=lambda: calls.append("inventory") or (True, [], ""),
        update_purchase_by_id=lambda *_args, **_kwargs: calls.append("host-update") or True,
    )

    assert received == 0
    assert calls == []


def test_unsafe_ownership_classification_fails_closed_before_side_effects(
    monkeypatch,
):
    purchases = [_legacy_purchase()]
    calls = []

    monkeypatch.setattr(
        receive_flow,
        "classify_host_purchases",
        lambda rows: _ownership_decisions(rows, HostPurchaseOwnership.UNSAFE),
    )

    received = receive_flow.try_receive_once(
        get_purchases=lambda: [dict(row) for row in purchases],
        update_purchase=lambda *_args, **_kwargs: calls.append("host-update") or True,
        get_buff_client=lambda: calls.append("buff-client") or object(),
        get_steam_credentials=lambda: calls.append("credentials") or {},
        scan_inventory=lambda: calls.append("inventory") or (True, [], ""),
        update_purchase_by_id=lambda *_args, **_kwargs: calls.append("host-update") or True,
    )

    assert received == 0
    assert calls == []


def test_unavailable_ownership_classification_fails_closed_before_side_effects(
    monkeypatch,
):
    purchases = [_legacy_purchase()]
    calls = []

    def unavailable(_rows):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(receive_flow, "classify_host_purchases", unavailable)

    received = receive_flow.try_receive_once(
        get_purchases=lambda: [dict(row) for row in purchases],
        update_purchase=lambda *_args, **_kwargs: calls.append("host-update") or True,
        get_buff_client=lambda: calls.append("buff-client") or object(),
        get_steam_credentials=lambda: calls.append("credentials") or {},
        scan_inventory=lambda: calls.append("inventory") or (True, [], ""),
        update_purchase_by_id=lambda *_args, **_kwargs: calls.append("host-update") or True,
    )

    assert received == 0
    assert calls == []


def test_mixed_legacy_and_managed_rows_only_legacy_row_can_be_received(monkeypatch):
    purchases = [
        _legacy_purchase(1),
        _legacy_purchase(2),
    ]
    calls = []
    accepted = {"value": False}
    task = {
        "tradeofferid": "offer-legacy",
        "created_at": 100,
        "items": [{"goods_id": 42, "market_hash_name": ITEM_NAME}],
    }

    def classify(rows):
        return [
            SimpleNamespace(ownership=HostPurchaseOwnership.UNOWNED),
            SimpleNamespace(ownership=HostPurchaseOwnership.MANAGED),
        ]

    def update_purchase_by_id(db_id, data):
        calls.append(("host-update", db_id))
        next(row for row in purchases if row["_db_id"] == db_id).update(data)
        return True

    def scan_inventory():
        if not accepted["value"]:
            return True, [], ""
        return True, [{"assetid": "receiver-asset", "market_hash_name": ITEM_NAME}], ""

    monkeypatch.setattr(receive_flow, "classify_host_purchases", classify)
    monkeypatch.setattr(
        receive_flow,
        "fetch_buff_steam_trade",
        lambda _client: calls.append("buff-fetch") or (True, [task], ""),
    )
    monkeypatch.setattr(
        receive_flow,
        "accept_steam_trade_offer",
        lambda *_args, **_kwargs: accepted.update(value=True) or calls.append("accept") or True,
    )
    monkeypatch.setattr(receive_flow, "jittered_sleep", lambda *_args, **_kwargs: None)

    received = receive_flow.try_receive_once(
        get_purchases=lambda: [dict(row) for row in purchases],
        update_purchase=lambda *_args, **_kwargs: False,
        get_buff_client=lambda: calls.append("buff-client") or object(),
        get_steam_credentials=lambda: calls.append("credentials") or {
            "cookies": "steamLoginSecure=secure; sessionid=session",
        },
        scan_inventory=scan_inventory,
        update_purchase_by_id=update_purchase_by_id,
    )

    assert received == 1
    assert calls.count("accept") == 1
    assert purchases[0]["assetid"] == "receiver-asset"
    assert not purchases[1].get("assetid")
    assert [call for call in calls if isinstance(call, tuple)] == [("host-update", 1)]


def test_reclassified_protected_row_is_not_accepted(monkeypatch):
    row = _legacy_purchase()
    snapshots = [
        [dict(row)],
        [dict(row, ownership_flip=True)],
    ]
    calls = []
    task = {
        "tradeofferid": "offer-raced",
        "created_at": 100,
        "items": [{"goods_id": 42, "market_hash_name": ITEM_NAME}],
    }

    def get_purchases():
        return snapshots.pop(0) if snapshots else [dict(row, ownership_flip=True)]

    def classify(rows):
        ownership = (
            HostPurchaseOwnership.MANAGED
            if rows[0].get("ownership_flip")
            else HostPurchaseOwnership.UNOWNED
        )
        return _ownership_decisions(rows, ownership)

    monkeypatch.setattr(receive_flow, "classify_host_purchases", classify)
    monkeypatch.setattr(
        receive_flow,
        "fetch_buff_steam_trade",
        lambda _client: calls.append("buff-fetch") or (True, [task], ""),
    )
    monkeypatch.setattr(
        receive_flow,
        "accept_steam_trade_offer",
        lambda *_args, **_kwargs: calls.append("accept") or True,
    )

    received = receive_flow.try_receive_once(
        get_purchases=get_purchases,
        update_purchase=lambda *_args, **_kwargs: calls.append("host-update") or True,
        get_buff_client=lambda: calls.append("buff-client") or object(),
        get_steam_credentials=lambda: calls.append("credentials") or {
            "cookies": "steamLoginSecure=secure; sessionid=session",
        },
        scan_inventory=lambda: calls.append("inventory") or (True, [], ""),
        update_purchase_by_id=lambda *_args, **_kwargs: calls.append("host-update") or True,
    )

    assert received == 0
    assert "accept" not in calls
    assert "inventory" not in calls


def test_accept_timeout_is_unknown_and_non_idempotent_post_is_not_retried(
    monkeypatch,
):
    calls = []

    class ProxyManager:
        def get_proxies_for_request(self, **_kwargs):
            return {}

    def timeout_once(*_args, **_kwargs):
        calls.append(True)
        raise requests.Timeout("response lost")

    monkeypatch.setattr(
        "utils.proxy_manager.get_proxy_manager",
        lambda: ProxyManager(),
    )
    monkeypatch.setattr(receive_flow.requests, "post", timeout_once)

    result = receive_flow.accept_steam_trade_offer(
        "offer-1",
        {"sessionid": "session"},
    )

    assert result is None
    assert calls == [True]


def test_exact_goods_id_match_beats_older_same_name_fallback():
    pending = [
        {
            "_db_id": 1,
            "goods_id": 999,
            "name": ITEM_NAME,
            "at": 1.0,
        },
        {
            "_db_id": 2,
            "goods_id": 42,
            "name": ITEM_NAME,
            "at": 2.0,
        },
    ]
    incoming = {
        "goods_id": 42,
        "market_hash_name": ITEM_NAME,
    }

    matched = receive_flow._match_purchase_for_item(incoming, pending, set())

    assert matched is not None
    assert matched["_db_id"] == 2


def test_ambiguous_name_only_match_is_rejected():
    pending = [
        {"_db_id": 1, "name": ITEM_NAME, "at": 1.0},
        {"_db_id": 2, "name": ITEM_NAME, "at": 2.0},
    ]
    incoming = {"market_hash_name": ITEM_NAME}

    assert (
        receive_flow._match_purchase_for_item(incoming, pending, set()) is None
    )


def test_partial_inventory_visibility_never_persists_seller_assetid(
    monkeypatch,
):
    purchases = [
        {
            "_db_id": 1,
            "goods_id": 42,
            "name": ITEM_NAME,
            "at": 1.0,
            "pending_receipt": True,
        },
        {
            "_db_id": 2,
            "goods_id": 42,
            "name": ITEM_NAME,
            "at": 2.0,
            "pending_receipt": True,
        },
    ]
    task = {
        "tradeofferid": "offer-1",
        "created_at": 100,
        "items": [
            {
                "assetid": "seller-old-asset-1",
                "goods_id": 42,
                "market_hash_name": ITEM_NAME,
            },
            {
                "assetid": "seller-old-asset-2",
                "goods_id": 42,
                "market_hash_name": ITEM_NAME,
            },
        ],
    }
    accepted = {"value": False}

    def get_purchases():
        return [dict(row) for row in purchases]

    def update_purchase_by_id(db_id, data):
        row = next(row for row in purchases if row["_db_id"] == db_id)
        row.update(data)
        return True

    def accept_offer(*_args, **_kwargs):
        accepted["value"] = True
        return True

    def scan_inventory():
        if not accepted["value"]:
            return True, [], ""
        # Steam inventory propagation is incomplete: only one of the two
        # received assets is visible so far.
        return (
            True,
            [
                {
                    "assetid": "receiver-new-asset-1",
                    "market_hash_name": ITEM_NAME,
                }
            ],
            "",
        )

    monkeypatch.setattr(
        receive_flow,
        "fetch_buff_steam_trade",
        lambda _client: (True, [task], ""),
    )
    monkeypatch.setattr(receive_flow, "accept_steam_trade_offer", accept_offer)
    monkeypatch.setattr(
        receive_flow,
        "jittered_sleep",
        lambda *_args, **_kwargs: None,
    )

    receive_flow.try_receive_once(
        get_purchases=get_purchases,
        update_purchase=lambda *_args, **_kwargs: False,
        get_buff_client=lambda: object(),
        get_steam_credentials=lambda: {
            "cookies": "steamLoginSecure=secure; sessionid=session",
        },
        scan_inventory=scan_inventory,
        update_purchase_by_id=update_purchase_by_id,
    )

    assigned = [row for row in purchases if row.get("assetid")]
    still_pending = [
        row
        for row in purchases
        if row.get("pending_receipt") and not row.get("assetid")
    ]

    assert [row["assetid"] for row in assigned] == ["receiver-new-asset-1"]
    assert [row["_db_id"] for row in still_pending] == [2]
    assert not {
        "seller-old-asset-1",
        "seller-old-asset-2",
    }.intersection(row.get("assetid") for row in purchases)


def test_inventory_poll_waits_for_all_new_assets_and_excludes_old_same_name(
    monkeypatch,
):
    purchases = [
        {
            "_db_id": 1,
            "goods_id": 42,
            "name": ITEM_NAME,
            "at": 1.0,
            "pending_receipt": True,
        },
        {
            "_db_id": 2,
            "goods_id": 42,
            "name": ITEM_NAME,
            "at": 2.0,
            "pending_receipt": True,
        },
    ]
    task = {
        "tradeofferid": "offer-1",
        "created_at": 100,
        "items": [
            {
                "assetid": "seller-old-asset-1",
                "goods_id": 42,
                "market_hash_name": ITEM_NAME,
            },
            {
                "assetid": "seller-old-asset-2",
                "goods_id": 42,
                "market_hash_name": ITEM_NAME,
            },
        ],
    }
    accepted = {"value": False}
    post_scans = {"value": 0}

    def get_purchases():
        return [dict(row) for row in purchases]

    def update_purchase_by_id(db_id, data):
        row = next(row for row in purchases if row["_db_id"] == db_id)
        row.update(data)
        return True

    def accept_offer(*_args, **_kwargs):
        accepted["value"] = True
        return True

    def inventory_item(assetid):
        return {"assetid": assetid, "market_hash_name": ITEM_NAME}

    def scan_inventory():
        existing = inventory_item("personal-existing-same-name")
        if not accepted["value"]:
            return True, [existing], ""
        post_scans["value"] += 1
        visible = [existing, inventory_item("receiver-new-asset-1")]
        if post_scans["value"] >= 2:
            visible.append(inventory_item("receiver-new-asset-2"))
        return True, visible, ""

    monkeypatch.setattr(
        receive_flow,
        "fetch_buff_steam_trade",
        lambda _client: (True, [task], ""),
    )
    monkeypatch.setattr(receive_flow, "accept_steam_trade_offer", accept_offer)
    monkeypatch.setattr(
        receive_flow,
        "jittered_sleep",
        lambda *_args, **_kwargs: None,
    )

    received = receive_flow.try_receive_once(
        get_purchases=get_purchases,
        update_purchase=lambda *_args, **_kwargs: False,
        get_buff_client=lambda: object(),
        get_steam_credentials=lambda: {
            "cookies": "steamLoginSecure=secure; sessionid=session",
        },
        scan_inventory=scan_inventory,
        update_purchase_by_id=update_purchase_by_id,
    )

    assert received == 2
    assert post_scans["value"] == 2
    assert [row["assetid"] for row in purchases] == [
        "receiver-new-asset-1",
        "receiver-new-asset-2",
    ]


def test_failed_pre_accept_inventory_snapshot_leaves_offer_unaccepted(
    monkeypatch,
):
    purchases = [
        {
            "_db_id": 1,
            "goods_id": 42,
            "name": ITEM_NAME,
            "at": 1.0,
            "pending_receipt": True,
        }
    ]
    task = {
        "tradeofferid": "offer-1",
        "created_at": 100,
        "items": [
            {
                "assetid": "seller-old-asset",
                "goods_id": 42,
                "market_hash_name": ITEM_NAME,
            }
        ],
    }
    accepted = []

    monkeypatch.setattr(
        receive_flow,
        "fetch_buff_steam_trade",
        lambda _client: (True, [task], ""),
    )
    monkeypatch.setattr(
        receive_flow,
        "accept_steam_trade_offer",
        lambda *_args, **_kwargs: accepted.append(True) or True,
    )

    received = receive_flow.try_receive_once(
        get_purchases=lambda: [dict(row) for row in purchases],
        update_purchase=lambda *_args, **_kwargs: False,
        get_buff_client=lambda: object(),
        get_steam_credentials=lambda: {
            "cookies": "steamLoginSecure=secure; sessionid=session",
        },
        scan_inventory=lambda: (False, [], "inventory unavailable"),
        update_purchase_by_id=lambda *_args, **_kwargs: True,
    )

    assert received == 0
    assert accepted == []
    assert purchases[0]["pending_receipt"] is True
    assert not purchases[0].get("assetid")


def test_unmatched_offer_is_never_accepted_or_scanned(monkeypatch):
    purchases = [
        {
            "_db_id": 1,
            "goods_id": 999,
            "name": ITEM_NAME,
            "at": 1.0,
            "pending_receipt": True,
        }
    ]
    task = {
        "tradeofferid": "offer-foreign",
        "created_at": 100,
        "items": [
            {
                "assetid": "seller-old-asset",
                "goods_id": 42,
                "market_hash_name": ITEM_NAME,
            }
        ],
    }
    accepted = []
    scans = []

    monkeypatch.setattr(
        receive_flow,
        "fetch_buff_steam_trade",
        lambda _client: (True, [task], ""),
    )
    monkeypatch.setattr(
        receive_flow,
        "accept_steam_trade_offer",
        lambda *_args, **_kwargs: accepted.append(True) or True,
    )

    received = receive_flow.try_receive_once(
        get_purchases=lambda: [dict(row) for row in purchases],
        update_purchase=lambda *_args, **_kwargs: False,
        get_buff_client=lambda: object(),
        get_steam_credentials=lambda: {
            "cookies": "steamLoginSecure=secure; sessionid=session",
        },
        scan_inventory=lambda: scans.append(True) or (True, [], ""),
        update_purchase_by_id=lambda *_args, **_kwargs: True,
    )

    assert received == 0
    assert accepted == []
    assert scans == []


def test_unknown_accept_result_is_reconciled_from_inventory(monkeypatch):
    purchases = [
        {
            "_db_id": 1,
            "goods_id": 42,
            "name": ITEM_NAME,
            "at": 1.0,
            "pending_receipt": True,
        }
    ]
    task = {
        "tradeofferid": "offer-unknown",
        "created_at": 100,
        "items": [
            {
                "assetid": "seller-old-asset",
                "goods_id": 42,
                "market_hash_name": ITEM_NAME,
            }
        ],
    }
    accepted = {"attempted": False}

    def get_purchases():
        return [dict(row) for row in purchases]

    def update_purchase_by_id(db_id, data):
        row = next(row for row in purchases if row["_db_id"] == db_id)
        row.update(data)
        return True

    def accept_unknown(*_args, **_kwargs):
        accepted["attempted"] = True
        return None

    def scan_inventory():
        if not accepted["attempted"]:
            return True, [], ""
        return (
            True,
            [
                {
                    "assetid": "receiver-new-asset",
                    "market_hash_name": ITEM_NAME,
                }
            ],
            "",
        )

    monkeypatch.setattr(
        receive_flow,
        "fetch_buff_steam_trade",
        lambda _client: (True, [task], ""),
    )
    monkeypatch.setattr(
        receive_flow,
        "accept_steam_trade_offer",
        accept_unknown,
    )
    monkeypatch.setattr(
        receive_flow,
        "jittered_sleep",
        lambda *_args, **_kwargs: None,
    )

    received = receive_flow.try_receive_once(
        get_purchases=get_purchases,
        update_purchase=lambda *_args, **_kwargs: False,
        get_buff_client=lambda: object(),
        get_steam_credentials=lambda: {
            "cookies": "steamLoginSecure=secure; sessionid=session",
        },
        scan_inventory=scan_inventory,
        update_purchase_by_id=update_purchase_by_id,
    )

    assert received == 1
    assert purchases[0]["assetid"] == "receiver-new-asset"
    assert purchases[0]["pending_receipt"] is False
