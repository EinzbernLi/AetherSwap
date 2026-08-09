import ast
from collections.abc import Mapping
from pathlib import Path

import pytest

from app.auto_offer import AUTO_OFFER_DEFAULT_ENABLED
from app.auto_offer.adapters import PlatformCapability
from app.auto_offer.contracts import DeliveryMode, DeliverySnapshot, DeliveryStatus
from app.auto_offer.host_readonly import (
    HostReadOnlyAutoOfferBridge,
    HostReadOnlyBridgeConfigurationError,
    build_host_readonly_auto_offer_bridge,
)
from app.auto_offer.runtime_readonly import (
    READONLY_RUNTIME_CAPABILITIES,
    ReadOnlyAutoOfferRuntime,
    ReadOnlyRuntimeConfigurationError,
)
from app.auto_offer.store import AutoOfferStore, AutoOfferStoreError, StoredDelivery


STEAM_ID = "76561198000000001"
OTHER_STEAM_ID = "76561198000000002"
TOKEN = "task021-fake-token"
COOKIE = f"sessionid=fake-session; steamLoginSecure={STEAM_ID}||{TOKEN}"
ACCOUNT_ID = "account-1"
OTHER_ACCOUNT_ID = "account-2"
BUFF_ORDER_ID = "123456"


class Tripwire:
    def __getattribute__(self, _name):
        raise AssertionError("disabled dependency was inspected")


class FakeSession:
    instances = []

    def __init__(self, *, verify=True):
        self.verify = verify
        self.get_calls = []
        self.write_calls = []
        self.closed = False
        type(self).instances.append(self)

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        raise AssertionError("unexpected platform GET")

    def _forbid_write(self, method, *_args, **_kwargs):
        self.write_calls.append(method)
        raise AssertionError(f"{method.upper()} forbidden")

    def post(self, *args, **kwargs):
        return self._forbid_write("post", *args, **kwargs)

    def put(self, *args, **kwargs):
        return self._forbid_write("put", *args, **kwargs)

    def patch(self, *args, **kwargs):
        return self._forbid_write("patch", *args, **kwargs)

    def delete(self, *args, **kwargs):
        return self._forbid_write("delete", *args, **kwargs)

    def close(self):
        self.closed = True


class UnsafeSession(FakeSession):
    def __init__(self):
        super().__init__(verify=False)


class FakeBuffClient:
    def __init__(self):
        self.calls = 0

    def get_steam_trades(self):
        self.calls += 1
        return []


class MissingBuffClient:
    pass


class NonCallableBuffClient:
    get_steam_trades = "not-callable"


class FakeCoordinator:
    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def step(self, delivery):
        self.calls.append(delivery)
        return self.result


class FakeOwnedStore:
    def __init__(self, *, initialize_error=None, recoverable=()):
        self.initialize_error = initialize_error
        self.initialize_calls = 0
        self.close_calls = 0
        self.recoverable = tuple(recoverable)
        self.ensure_calls = []

    def initialize(self):
        self.initialize_calls += 1
        if self.initialize_error is not None:
            raise self.initialize_error

    def close(self):
        self.close_calls += 1

    def ensure_initial(self, snapshot):
        self.ensure_calls.append(snapshot)
        return StoredDelivery(snapshot=snapshot, revision=1)

    def list_recoverable(self):
        return list(self.recoverable)


def _patch_session(monkeypatch, session_type=FakeSession):
    session_type.instances.clear()
    monkeypatch.setattr("app.auto_offer.host_readonly.requests.Session", session_type)


def _build(monkeypatch, tmp_path, **overrides):
    _patch_session(monkeypatch)
    values = {
        "enabled": True,
        "buff_client": FakeBuffClient(),
        "account_id": ACCOUNT_ID,
        "account_steam_id": STEAM_ID,
        "steam_credentials": {"steam_id": STEAM_ID, "cookies": COOKIE},
        "store_path": tmp_path / "auto_offer.db",
        "timeout_seconds": 5.0,
    }
    values.update(overrides)
    return build_host_readonly_auto_offer_bridge(**values)


def _record(**changes):
    value = {"buff_order_id": BUFF_ORDER_ID, "pending_receipt": True, "assetid": None}
    value.update(changes)
    return value


def _snapshot(purchase_id="p-1"):
    return DeliverySnapshot(
        purchase_id=purchase_id,
        buff_order_id="order-1",
        account_id=ACCOUNT_ID,
        recipient_steam_id=STEAM_ID,
        delivery_mode=None,
        delivery_status=DeliveryStatus.PENDING_DIRECTION,
        steam_tradeoffer_id=None,
        offer_attempted_at=None,
        offer_sent_at=None,
        received_at=None,
        delivery_error=None,
        pending_receipt=True,
        assetid=None,
    )


def test_default_off_is_exact_and_inspects_nothing(monkeypatch):
    _patch_session(monkeypatch)
    assert AUTO_OFFER_DEFAULT_ENABLED is False
    tripwire = Tripwire()
    assert build_host_readonly_auto_offer_bridge(
        store_path=tripwire,
        buff_client=tripwire,
        account_id=tripwire,
        account_steam_id=tripwire,
        steam_credentials=tripwire,
        timeout_seconds=tripwire,
    ) is None
    assert FakeSession.instances == []


@pytest.mark.parametrize("enabled", [0, 1, None, "true", object()])
def test_enabled_requires_exact_bool(monkeypatch, enabled):
    _patch_session(monkeypatch)
    with pytest.raises(HostReadOnlyBridgeConfigurationError, match="enabled_must_be_bool"):
        build_host_readonly_auto_offer_bridge(enabled=enabled)
    assert FakeSession.instances == []


@pytest.mark.parametrize("steam_id", [None, "", "0", "01", " 1", 1])
def test_noncanonical_host_steam_id_blocks_before_session(monkeypatch, tmp_path, steam_id):
    _patch_session(monkeypatch)
    with pytest.raises(HostReadOnlyBridgeConfigurationError, match="account_steam_id"):
        build_host_readonly_auto_offer_bridge(
            enabled=True,
            buff_client=FakeBuffClient(),
            account_id=ACCOUNT_ID,
            account_steam_id=steam_id,
            steam_credentials={"steam_id": STEAM_ID, "cookies": COOKIE},
            store_path=tmp_path / "db",
        )
    assert FakeSession.instances == []


def test_host_and_credential_identity_mismatch_blocks_before_session(monkeypatch, tmp_path):
    _patch_session(monkeypatch)
    with pytest.raises(HostReadOnlyBridgeConfigurationError, match="steam_identity_mismatch"):
        build_host_readonly_auto_offer_bridge(
            enabled=True,
            buff_client=FakeBuffClient(),
            account_id=ACCOUNT_ID,
            account_steam_id=STEAM_ID,
            steam_credentials={"steam_id": OTHER_STEAM_ID, "cookies": COOKIE},
            store_path=tmp_path / "db",
        )
    assert FakeSession.instances == []


@pytest.mark.parametrize("credential_steam_id", [None, "", "0", "01", " 1", 1])
def test_noncanonical_credential_steam_id_blocks_before_session(
    monkeypatch, tmp_path, credential_steam_id
):
    _patch_session(monkeypatch)
    with pytest.raises(
        HostReadOnlyBridgeConfigurationError, match="credential_steam_id"
    ):
        build_host_readonly_auto_offer_bridge(
            enabled=True,
            buff_client=FakeBuffClient(),
            account_id=ACCOUNT_ID,
            account_steam_id=STEAM_ID,
            steam_credentials={"steam_id": credential_steam_id, "cookies": COOKIE},
            store_path=tmp_path / "db",
        )
    assert FakeSession.instances == []


@pytest.mark.parametrize(
    "credentials",
    [
        {},
        {"steam_id": STEAM_ID},
        {"steam_id": STEAM_ID, "cookies": ""},
        {"steam_id": STEAM_ID, "cookies": "sessionid=x"},
        {"steam_id": STEAM_ID, "cookies": "steamLoginSecure=malformed"},
    ],
)
def test_missing_or_malformed_credentials_fail_closed_without_get(
    monkeypatch, tmp_path, credentials
):
    _patch_session(monkeypatch)
    with pytest.raises(HostReadOnlyBridgeConfigurationError):
        build_host_readonly_auto_offer_bridge(
            enabled=True,
            buff_client=FakeBuffClient(),
            account_id=ACCOUNT_ID,
            account_steam_id=STEAM_ID,
            steam_credentials=credentials,
            store_path=tmp_path / "db",
        )
    assert all(not session.get_calls for session in FakeSession.instances)


def test_cookie_bound_identity_mismatch_is_checked_before_get(monkeypatch, tmp_path):
    _patch_session(monkeypatch)
    bad_cookie = f"steamLoginSecure={OTHER_STEAM_ID}||{TOKEN}"
    with pytest.raises(HostReadOnlyBridgeConfigurationError, match="readonly_runtime"):
        build_host_readonly_auto_offer_bridge(
            enabled=True,
            buff_client=FakeBuffClient(),
            account_id=ACCOUNT_ID,
            account_steam_id=STEAM_ID,
            steam_credentials={"steam_id": STEAM_ID, "cookies": bad_cookie},
            store_path=tmp_path / "db",
        )
    assert len(FakeSession.instances) == 1
    assert FakeSession.instances[0].get_calls == []
    assert FakeSession.instances[0].closed is True


@pytest.mark.parametrize("buff_client", [None, MissingBuffClient(), NonCallableBuffClient()])
def test_buff_readonly_dependency_is_required_and_not_called(monkeypatch, tmp_path, buff_client):
    _patch_session(monkeypatch)
    with pytest.raises(HostReadOnlyBridgeConfigurationError, match="buff"):
        build_host_readonly_auto_offer_bridge(
            enabled=True,
            buff_client=buff_client,
            account_id=ACCOUNT_ID,
            account_steam_id=STEAM_ID,
            steam_credentials={"steam_id": STEAM_ID, "cookies": COOKIE},
            store_path=tmp_path / "db",
        )
    assert FakeSession.instances == []


def test_valid_construction_initializes_store_and_performs_no_io(monkeypatch, tmp_path):
    buff = FakeBuffClient()
    bridge = _build(monkeypatch, tmp_path, buff_client=buff)
    try:
        assert bridge.account_id == ACCOUNT_ID
        assert bridge.recipient_steam_id == STEAM_ID
        assert bridge.capabilities == READONLY_RUNTIME_CAPABILITIES
        assert bridge.capabilities == frozenset(
            {
                PlatformCapability.READ_DELIVERY_DIRECTION,
                PlatformCapability.READ_OFFER_STATE,
                PlatformCapability.READ_STEAM_TRADE_OFFER,
                PlatformCapability.READ_STEAM_COMPLETED_TRADE,
            }
        )
        assert PlatformCapability.READ_INVENTORY_STATE not in bridge.capabilities
        assert PlatformCapability.SEND_OFFER not in bridge.capabilities
        assert len(FakeSession.instances) == 1
        assert FakeSession.instances[0].verify is not False
        assert FakeSession.instances[0].get_calls == []
        assert buff.calls == 0
        assert (tmp_path / "auto_offer.db").exists()
    finally:
        bridge.close()


def test_unsafe_tls_session_is_rejected_and_closed(monkeypatch, tmp_path):
    _patch_session(monkeypatch, UnsafeSession)
    with pytest.raises(HostReadOnlyBridgeConfigurationError, match="tls"):
        build_host_readonly_auto_offer_bridge(
            enabled=True,
            buff_client=FakeBuffClient(),
            account_id=ACCOUNT_ID,
            account_steam_id=STEAM_ID,
            steam_credentials={"steam_id": STEAM_ID, "cookies": COOKIE},
            store_path=tmp_path / "db",
        )
    assert len(UnsafeSession.instances) == 1
    assert UnsafeSession.instances[0].closed is True


def test_store_initialization_failure_closes_session(monkeypatch, tmp_path):
    _patch_session(monkeypatch)
    owned = FakeOwnedStore(initialize_error=AutoOfferStoreError("init failed"))
    monkeypatch.setattr("app.auto_offer.host_readonly.AutoOfferStore", lambda _path: owned)
    with pytest.raises(HostReadOnlyBridgeConfigurationError, match="store_initialization"):
        build_host_readonly_auto_offer_bridge(
            enabled=True,
            buff_client=FakeBuffClient(),
            account_id=ACCOUNT_ID,
            account_steam_id=STEAM_ID,
            steam_credentials={"steam_id": STEAM_ID, "cookies": COOKIE},
            store_path=tmp_path / "db",
        )
    assert owned.initialize_calls == 1
    assert owned.close_calls == 1
    assert FakeSession.instances[0].closed is True


def test_runtime_failure_closes_initialized_store_and_session(monkeypatch, tmp_path):
    _patch_session(monkeypatch)
    owned = FakeOwnedStore()
    monkeypatch.setattr("app.auto_offer.host_readonly.AutoOfferStore", lambda _path: owned)
    monkeypatch.setattr(
        "app.auto_offer.host_readonly.build_readonly_auto_offer_runtime",
        lambda **_kwargs: (_ for _ in ()).throw(
            ReadOnlyRuntimeConfigurationError("runtime failed")
        ),
    )
    with pytest.raises(HostReadOnlyBridgeConfigurationError, match="readonly_runtime"):
        build_host_readonly_auto_offer_bridge(
            enabled=True,
            buff_client=FakeBuffClient(),
            account_id=ACCOUNT_ID,
            account_steam_id=STEAM_ID,
            steam_credentials={"steam_id": STEAM_ID, "cookies": COOKIE},
            store_path=tmp_path / "db",
        )
    assert owned.initialize_calls == 1
    assert owned.close_calls == 1
    assert FakeSession.instances[0].closed is True


def test_repr_is_secret_safe_and_close_is_idempotent(monkeypatch, tmp_path):
    bridge = _build(monkeypatch, tmp_path)
    session = FakeSession.instances[0]
    text = repr(bridge)
    assert ACCOUNT_ID in text and STEAM_ID in text and "capabilities=4" in text
    assert TOKEN not in text and COOKIE not in text and "fake-session" not in text
    bridge.close()
    bridge.close()
    assert session.closed is True
    with pytest.raises(HostReadOnlyBridgeConfigurationError, match="closed"):
        bridge.list_recoverable()


def test_valid_registration_is_exact_deterministic_and_does_not_mutate_input(
    monkeypatch, tmp_path
):
    record = _record()
    original = dict(record)
    bridge = _build(monkeypatch, tmp_path)
    try:
        stored = bridge.register_committed_purchase(record)
        snapshot = stored.snapshot
        assert record == original
        assert snapshot.purchase_id == "buff:123456"
        assert snapshot.buff_order_id == BUFF_ORDER_ID
        assert snapshot.account_id == ACCOUNT_ID
        assert snapshot.recipient_steam_id == STEAM_ID
        assert snapshot.delivery_mode is None
        assert snapshot.delivery_status is DeliveryStatus.PENDING_DIRECTION
        assert snapshot.steam_tradeoffer_id is None
        assert snapshot.offer_attempted_at is None
        assert snapshot.offer_sent_at is None
        assert snapshot.received_at is None
        assert snapshot.delivery_error is None
        assert snapshot.pending_receipt is True
        assert snapshot.assetid is None
        assert FakeSession.instances[0].get_calls == []
    finally:
        bridge.close()


def test_duplicate_registration_is_idempotent(monkeypatch, tmp_path):
    bridge = _build(monkeypatch, tmp_path)
    try:
        first = bridge.register_committed_purchase(_record())
        second = bridge.register_committed_purchase(_record())
        assert second == first
    finally:
        bridge.close()


@pytest.mark.parametrize(
    "record",
    [
        {"buff_order_id": BUFF_ORDER_ID, "pending_receipt": True},
        {"buff_order_id": BUFF_ORDER_ID, "pending_receipt": True, "assetid": ""},
    ],
)
def test_registration_allows_missing_or_empty_assetid(monkeypatch, tmp_path, record):
    bridge = _build(monkeypatch, tmp_path)
    try:
        stored = bridge.register_committed_purchase(record)
        assert stored.snapshot.assetid is None
        assert stored.snapshot.pending_receipt is True
    finally:
        bridge.close()


@pytest.mark.parametrize(
    "record, message",
    [
        ({"buff_order_id": "", "pending_receipt": True, "assetid": None}, "buff_order"),
        ({"buff_order_id": " 123456", "pending_receipt": True, "assetid": None}, "buff_order"),
        ({"buff_order_id": "123456 ", "pending_receipt": True, "assetid": None}, "buff_order"),
        ({"buff_order_id": "123456", "pending_receipt": False, "assetid": None}, "pending"),
        ({"buff_order_id": "123456", "pending_receipt": 1, "assetid": None}, "pending"),
        ({"buff_order_id": "123456", "pending_receipt": "true", "assetid": None}, "pending"),
        ({"buff_order_id": "123456", "pending_receipt": True, "assetid": "9001"}, "asset"),
    ],
)
def test_invalid_purchase_records_fail_closed_without_platform_io(
    monkeypatch, tmp_path, record, message
):
    bridge = _build(monkeypatch, tmp_path)
    try:
        with pytest.raises(HostReadOnlyBridgeConfigurationError, match=message):
            bridge.register_committed_purchase(record)
        assert FakeSession.instances[0].get_calls == []
    finally:
        bridge.close()


def test_registration_conflict_fails_closed(monkeypatch, tmp_path):
    path = tmp_path / "shared.db"
    first = _build(monkeypatch, tmp_path, store_path=path)
    second = _build(
        monkeypatch,
        tmp_path,
        store_path=path,
        account_id=OTHER_ACCOUNT_ID,
    )
    try:
        first.register_committed_purchase(_record())
        with pytest.raises(HostReadOnlyBridgeConfigurationError, match="registration"):
            second.register_committed_purchase(_record())
    finally:
        first.close()
        second.close()


def test_list_recoverable_returns_store_order_without_step_or_platform_io():
    first = StoredDelivery(snapshot=_snapshot("p-1"), revision=1)
    second = StoredDelivery(snapshot=_snapshot("p-2"), revision=1)
    store = FakeOwnedStore(recoverable=[first, second])
    coordinator = FakeCoordinator(result=object())
    runtime = ReadOnlyAutoOfferRuntime(coordinator)
    session = FakeSession()
    bridge = HostReadOnlyAutoOfferBridge(
        store=store,
        runtime=runtime,
        session=session,
        account_id=ACCOUNT_ID,
        recipient_steam_id=STEAM_ID,
    )
    try:
        assert bridge.list_recoverable() == (first, second)
        assert coordinator.calls == []
        assert session.get_calls == []
    finally:
        bridge.close()


def test_step_delegates_exactly_once():
    delivery = object()
    result = object()
    store = FakeOwnedStore()
    coordinator = FakeCoordinator(result=result)
    bridge = HostReadOnlyAutoOfferBridge(
        store=store,
        runtime=ReadOnlyAutoOfferRuntime(coordinator),
        session=FakeSession(),
        account_id=ACCOUNT_ID,
        recipient_steam_id=STEAM_ID,
    )
    try:
        assert bridge.step(delivery) is result
        assert coordinator.calls == [delivery]
    finally:
        bridge.close()


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def test_static_source_has_no_legacy_or_background_or_write_paths():
    path = Path(__file__).resolve().parents[1] / "app" / "auto_offer" / "host_readonly.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden_imports = {
        "app.accounts",
        "app.config_loader",
        "config",
        "app.pipeline",
        "app.pipeline_steps",
        "app.services.workers",
        "app.receive_flow",
        "app.api",
        "app.main",
        "steam.session",
    }
    imports = set()
    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
        elif isinstance(node, ast.Call):
            dotted = _dotted(node.func)
            if dotted:
                calls.add(dotted)
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "post",
                "put",
                "patch",
                "delete",
            }:
                calls.add(node.func.attr)
    assert imports.isdisjoint(forbidden_imports)
    assert not calls.intersection(
        {
            "steam.session.create_market_session",
            "app.receive_flow.accept_steam_trade_offer",
            "requests.post",
            "requests.put",
            "requests.patch",
            "requests.delete",
            "post",
            "put",
            "patch",
            "delete",
            "time.sleep",
            "Thread",
            "Timer",
            "run_forever",
            "poll",
        }
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                assert not (
                    isinstance(target, ast.Attribute)
                    and target.attr == "verify"
                    and isinstance(node.value, ast.Constant)
                    and node.value.value is False
                )


def test_package_does_not_auto_export_or_attach_bridge():
    import app.auto_offer as package

    assert package.AUTO_OFFER_DEFAULT_ENABLED is False
    assert not hasattr(package, "HostReadOnlyAutoOfferBridge")
    assert not hasattr(package, "build_host_readonly_auto_offer_bridge")
