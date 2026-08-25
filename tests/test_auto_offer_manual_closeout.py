from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

import app.auto_offer.manual_closeout as closeout
from app.auto_offer.contracts import DeliveryMode, DeliverySnapshot, DeliveryStatus
from app.auto_offer.recovery_command import RecoveryTargetBinding
from app.auto_offer.store import StoredDelivery


ACCOUNT = "account-1"
RECIPIENT = "76561198000000001"
COUNTERPARTY = "76561198000000002"
ORDER = "order-1"
PURCHASE = f"buff:{ORDER}"
OFFER = "9001"


def _stored() -> StoredDelivery:
    return StoredDelivery(
        DeliverySnapshot(
            purchase_id=PURCHASE,
            buff_order_id=ORDER,
            account_id=ACCOUNT,
            recipient_steam_id=RECIPIENT,
            delivery_mode=DeliveryMode.BUYER_SENDS_OFFER,
            delivery_status=DeliveryStatus.RESULT_UNKNOWN,
            steam_tradeoffer_id=None,
            offer_attempted_at=10.0,
            offer_sent_at=None,
            received_at=None,
            delivery_error="write_result_unknown",
            pending_receipt=True,
            assetid=None,
            counterparty_steam_id=None,
        ),
        revision=4,
    )


def _binding() -> RecoveryTargetBinding:
    return RecoveryTargetBinding(
        source_commit="a" * 40,
        source_tree="b" * 40,
        fingerprint="f" * 64,
        order_id=ORDER,
        host_db_id=7,
        store=_stored(),
        account_id=ACCOUNT,
        recipient_steam_id=RECIPIENT,
        steam_cookie="steam-cookie",
        buff_cookie="buff-cookie",
        buff_user_agent="ua",
        buff_generation=3,
    )


class Reader:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.payload


def _offer_payload(**changes):
    payload = {
        "steam_tradeoffer_id": OFFER,
        "account_steam_id": RECIPIENT,
        "counterparty_steam_id": COUNTERPARTY,
        "is_our_offer": True,
        "lifecycle": "accepted",
        "items_to_give": [],
        "items_to_receive": [
            {
                "appid": 730,
                "contextid": "2",
                "assetid": "seller-asset",
                "amount": 1,
            }
        ],
    }
    payload.update(changes)
    return payload


def _completed_payload(**changes):
    payload = {
        "steam_tradeoffer_id": OFFER,
        "steam_trade_id": "8001",
        "account_steam_id": RECIPIENT,
        "counterparty_steam_id": COUNTERPARTY,
        "completed_at": 12.0,
        "items_given": [],
        "items_received": [
            {
                "appid": 730,
                "contextid": "2",
                "assetid": "seller-asset",
                "amount": 1,
                "new_contextid": "2",
                "new_assetid": "new-asset",
            }
        ],
        "inventory_confirmed_items": [],
    }
    payload.update(changes)
    return payload


def _proof():
    return closeout.collect_manual_closeout_proof(
        _binding(),
        OFFER,
        trade_offer_reader=Reader(_offer_payload()),
        completed_trade_reader=Reader(_completed_payload()),
    )


def test_proof_accepts_exact_completed_receipt_even_if_asset_was_later_sold():
    offer_reader = Reader(_offer_payload())
    completed_reader = Reader(_completed_payload(inventory_confirmed_items=[]))

    proof = closeout.collect_manual_closeout_proof(
        _binding(),
        OFFER,
        trade_offer_reader=offer_reader,
        completed_trade_reader=completed_reader,
    )

    assert proof.offer.is_our_offer is True
    assert proof.completed.items_received[0].new_assetid == "new-asset"
    assert proof.completed.inventory_confirmed_items == ()
    assert offer_reader.calls == [(OFFER,)]
    assert completed_reader.calls == [(OFFER, RECIPIENT)]


def test_proof_requires_outgoing_buyer_offer():
    with pytest.raises(
        closeout.RecoveryCommandError,
        match="steam_trade_offer_identity_mismatch",
    ):
        closeout.collect_manual_closeout_proof(
            _binding(),
            OFFER,
            trade_offer_reader=Reader(_offer_payload(is_our_offer=False)),
            completed_trade_reader=Reader(_completed_payload()),
        )


def test_proof_requires_same_source_item_across_offer_and_receipt():
    different = _completed_payload()
    different["items_received"][0]["assetid"] = "other-seller-asset"
    with pytest.raises(
        closeout.RecoveryCommandError,
        match="steam_completed_trade_identity_mismatch",
    ):
        closeout.collect_manual_closeout_proof(
            _binding(),
            OFFER,
            trade_offer_reader=Reader(_offer_payload()),
            completed_trade_reader=Reader(different),
        )


def test_proof_rejects_inventory_evidence_for_a_different_post_trade_asset():
    with pytest.raises(
        closeout.RecoveryCommandError,
        match="steam_inventory_identity_mismatch",
    ):
        closeout.collect_manual_closeout_proof(
            _binding(),
            OFFER,
            trade_offer_reader=Reader(_offer_payload()),
            completed_trade_reader=Reader(
                _completed_payload(
                    inventory_confirmed_items=[
                        {
                            "appid": 730,
                            "contextid": "2",
                            "assetid": "other-new-asset",
                            "amount": 1,
                        }
                    ]
                )
            ),
        )


def test_proof_rejects_completion_before_recorded_send_attempt():
    with pytest.raises(
        closeout.RecoveryCommandError,
        match="steam_completed_time_precedes_attempt",
    ):
        closeout.collect_manual_closeout_proof(
            _binding(),
            OFFER,
            trade_offer_reader=Reader(_offer_payload()),
            completed_trade_reader=Reader(_completed_payload(completed_at=9.0)),
        )


def test_invalid_offer_id_stops_before_readers():
    reader = Reader(_offer_payload())
    with pytest.raises(
        closeout.RecoveryCommandError,
        match="steam_tradeoffer_id_invalid",
    ):
        closeout.collect_manual_closeout_proof(
            _binding(),
            "09001",
            trade_offer_reader=reader,
            completed_trade_reader=Reader(_completed_payload()),
        )
    assert reader.calls == []


def test_targets_use_exact_completion_as_recovery_proof_time_and_receipt_time():
    targets = closeout._targets(_binding(), _proof())
    sent, confirmed, awaiting, received = targets
    assert [target.delivery_status for target in targets] == [
        DeliveryStatus.OFFER_SENT,
        DeliveryStatus.OFFER_CONFIRMED,
        DeliveryStatus.AWAITING_INVENTORY,
        DeliveryStatus.RECEIVED,
    ]
    assert sent.steam_tradeoffer_id == OFFER
    assert sent.counterparty_steam_id == COUNTERPARTY
    assert sent.offer_sent_at == 12.0
    assert confirmed.offer_sent_at == 12.0
    assert awaiting.offer_sent_at == 12.0
    assert received.received_at == 12.0
    assert received.assetid == "new-asset"
    assert received.pending_receipt is False


def test_execute_advances_exact_four_local_cas_then_one_host_receipt(monkeypatch):
    binding = _binding()
    proof = _proof()
    advanced = []
    receipt_calls = []
    host_checks = []

    monkeypatch.setattr(closeout, "_assert_binding_stable", lambda _binding: None)
    monkeypatch.setattr(
        closeout,
        "_assert_store_preexecution_stable",
        lambda _binding: None,
    )
    monkeypatch.setattr(
        closeout,
        "collect_manual_closeout_proof",
        lambda *_args, **_kwargs: proof,
    )
    monkeypatch.setattr(
        closeout,
        "_assert_exact_host_order_readonly",
        lambda path, **kwargs: host_checks.append((path, kwargs)),
    )

    class FakeStore:
        def __init__(self, path):
            self.path = path
            self.current = binding.store
            self.closed = False

        def initialize_existing(self):
            pass

        def get_by_buff_order_id(self, order_id):
            assert order_id == ORDER
            return self.current

        def advance(self, current, target):
            assert current == self.current
            advanced.append(target)
            self.current = StoredDelivery(target, current.revision + 1)
            return self.current

        def close(self):
            self.closed = True

    monkeypatch.setattr(closeout, "AutoOfferStore", FakeStore)

    def receipt_writer(db_id, order_id, assetid):
        receipt_calls.append((db_id, order_id, assetid))
        return True

    final = closeout.execute_manual_closeout(
        binding,
        expected_fingerprint="f" * 64,
        steam_tradeoffer_id=OFFER,
        store_path=SimpleNamespace(),
        host_db_path=SimpleNamespace(),
        receipt_writer=receipt_writer,
    )

    assert [target.delivery_status for target in advanced] == [
        DeliveryStatus.OFFER_SENT,
        DeliveryStatus.OFFER_CONFIRMED,
        DeliveryStatus.AWAITING_INVENTORY,
        DeliveryStatus.RECEIVED,
    ]
    assert final.revision == 8
    assert final.snapshot.assetid == "new-asset"
    assert receipt_calls == [(7, ORDER, "new-asset")]
    assert len(host_checks) == 1
    assert host_checks[0][1] == {
        "order_id": ORDER,
        "expected_db_id": 7,
        "expected_pending": True,
        "expected_assetid": None,
    }


def test_fingerprint_mismatch_stops_before_local_or_network(monkeypatch):
    monkeypatch.setattr(
        closeout,
        "_assert_binding_stable",
        lambda _binding: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    monkeypatch.setattr(
        closeout,
        "collect_manual_closeout_proof",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not read")),
    )
    with pytest.raises(
        closeout.RecoveryCommandError,
        match="target_fingerprint_mismatch",
    ):
        closeout.execute_manual_closeout(
            _binding(),
            expected_fingerprint="e" * 64,
            steam_tradeoffer_id=OFFER,
        )


def test_host_failure_is_reported_after_store_received(monkeypatch):
    binding = _binding()
    proof = _proof()
    monkeypatch.setattr(closeout, "_assert_binding_stable", lambda _binding: None)
    monkeypatch.setattr(
        closeout,
        "_assert_store_preexecution_stable",
        lambda _binding: None,
    )
    monkeypatch.setattr(
        closeout,
        "collect_manual_closeout_proof",
        lambda *_args, **_kwargs: proof,
    )
    monkeypatch.setattr(
        closeout,
        "_assert_exact_host_order_readonly",
        lambda *_args, **_kwargs: None,
    )

    class FakeStore:
        def __init__(self, _path):
            self.current = binding.store

        def initialize_existing(self):
            pass

        def get_by_buff_order_id(self, _order):
            return self.current

        def advance(self, current, target):
            self.current = StoredDelivery(target, current.revision + 1)
            return self.current

        def close(self):
            pass

    monkeypatch.setattr(closeout, "AutoOfferStore", FakeStore)
    with pytest.raises(
        closeout.RecoveryCommandError,
        match="host_receipt_completion_failed_after_store_received",
    ):
        closeout.execute_manual_closeout(
            binding,
            expected_fingerprint="f" * 64,
            steam_tradeoffer_id=OFFER,
            receipt_writer=lambda *_args: False,
        )
