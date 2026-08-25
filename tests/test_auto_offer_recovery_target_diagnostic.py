from types import SimpleNamespace

import app.auto_offer.recovery_target_diagnostic as diagnostic


def _binding():
    snapshot = SimpleNamespace(
        purchase_id="buff:order-1",
        buff_order_id="order-1",
        account_id="account-1",
        recipient_steam_id="76561198000000000",
    )
    return SimpleNamespace(
        account_id="account-1",
        fingerprint="a" * 64,
        store=SimpleNamespace(revision=4, snapshot=snapshot),
    )


def _history_page(items, *, page_num=1, total_page=1, page_size=10):
    return {
        "code": "OK",
        "data": {
            "page_num": page_num,
            "page_size": page_size,
            "total_page": total_page,
            "items": items,
        },
    }


def _target(**changes):
    value = {
        "id": "order-1",
        "seller_steam_id": "76561198000000001",
        "items_to_trade": [{"goods_id": 123, "assetid": "asset-secret"}],
        "trade_offer_url": "https://steamcommunity.com/tradeoffer/999999/?token=secret-token",
    }
    value.update(changes)
    return value


def test_target_shapes_are_sanitized_and_preserve_only_exact_field_classes():
    target = _target()
    seller = diagnostic._seller_shape(target)
    items = diagnostic._items_shape(target)
    url = diagnostic._trade_offer_url_shape(target)

    assert seller == (
        "seller_steam_id=string_canonical|seller_steamid=absent|relation=single"
    )
    assert items == (
        "items_to_trade=list_count_1|item0=mapping|"
        "goods_id=int_canonical|goods_id_relation=positive_decimal|"
        "assetid=string_canonical|assetid_relation=canonical"
    )
    assert url == (
        "trade_offer_url=string_canonical|kind=steam_view_offer_with_id"
    )
    combined = f"{seller} {items} {url}"
    assert "765611" not in combined
    assert "asset-secret" not in combined
    assert "999999" not in combined
    assert "secret-token" not in combined


def test_target_shapes_distinguish_absent_null_and_new_offer_link():
    target = _target(
        seller_steam_id=None,
        items_to_trade=None,
        trade_offer_url=(
            "https://steamcommunity.com/tradeoffer/new/?partner=123&token=hidden"
        ),
    )
    assert diagnostic._seller_shape(target) == (
        "seller_steam_id=null|seller_steamid=absent|relation=invalid"
    )
    assert diagnostic._items_shape(target) == "items_to_trade=null"
    assert diagnostic._trade_offer_url_shape(target) == (
        "trade_offer_url=string_canonical|kind=steam_new_offer_link"
    )


def test_target_shapes_distinguish_multiple_items_without_leaking_values():
    target = _target(
        items_to_trade=[
            {"goods_id": 111, "assetid": "asset-a"},
            {"goods_id": 222, "assetid": "asset-b"},
        ]
    )
    value = diagnostic._items_shape(target)
    assert value == "items_to_trade=list_count_2"
    assert "111" not in value
    assert "222" not in value
    assert "asset-a" not in value


def test_page_target_requires_exact_unique_order_and_valid_envelope():
    target, total = diagnostic._page_target(
        _history_page([{"id": "other"}, _target()], total_page=3),
        expected_page_num=1,
        target_order_id="order-1",
    )
    assert target is not None
    assert target["id"] == "order-1"
    assert total == 3


def test_diagnose_stops_on_first_exact_page_and_closes_client(monkeypatch):
    class Client:
        def __init__(self):
            self.calls = []
            self.closed = False

        def get_buy_order_history_page(self, page_num, game="csgo"):
            self.calls.append((page_num, game))
            if page_num == 1:
                return _history_page([{"id": "other"}], page_num=1, total_page=3)
            return _history_page([_target()], page_num=2, total_page=3)

        def close(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(diagnostic, "_make_buff_client", lambda _binding: client)
    result = diagnostic.diagnose_target_fields(_binding())

    assert result.target_page == 2
    assert result.history_requests == 2
    assert client.calls == [(1, "csgo"), (2, "csgo")]
    assert client.closed is True


def test_main_fingerprint_mismatch_stops_before_buff_client(monkeypatch, capsys):
    binding = _binding()
    monkeypatch.setattr(
        diagnostic,
        "collect_recovery_preflight",
        lambda **_kwargs: binding,
    )
    monkeypatch.setattr(
        diagnostic,
        "diagnose_target_fields",
        lambda _binding: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    code = diagnostic.main(
        [
            "--expected-commit",
            "c" * 40,
            "--expected-tree",
            "d" * 40,
            "--expected-fingerprint",
            "b" * 64,
        ]
    )
    assert code == 2
    assert capsys.readouterr().out.strip() == (
        "TASK049_BUFF_TARGET_FIELD_DIAGNOSTIC_BLOCKED "
        "reason=target_fingerprint_mismatch"
    )
