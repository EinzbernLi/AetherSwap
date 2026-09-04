from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8", newline="\n")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, got {count}")
    return text.replace(old, new, 1)


def replace_between(text: str, start: str, end: str, replacement: str, label: str) -> str:
    left = text.find(start)
    right = text.find(end, left + len(start))
    if left < 0 or right < 0:
        raise RuntimeError(f"{label}: anchors missing")
    return text[:left] + replacement + text[right:]


# ---------------------------------------------------------------------------
# canary_authority.py: buyer permits may intentionally have no pre-SEND
# counterparty; seller permits still require one. Owner fence also admits the
# already-existing exact seller ACCEPT path.
# ---------------------------------------------------------------------------
path = "app/auto_offer/canary_authority.py"
s = read(path)
s = replace_once(
    s,
    '_ALLOWED_OWNER_ACTIONS = frozenset({"auto_offer_send", "auto_offer_confirm", "host_receipt"})\n_NORMAL_ONLY_ACTIONS = frozenset({"auto_offer_accept"})',
    '_ALLOWED_OWNER_ACTIONS = frozenset({"auto_offer_send", "auto_offer_confirm", "auto_offer_accept", "host_receipt"})\n_NORMAL_ONLY_ACTIONS = frozenset()',
    "authority owner actions",
)
s = replace_once(
    s,
    '    expected_counterparty_steam_id: str\n    expected_is_our_offer: bool',
    '    expected_counterparty_steam_id: str | None\n    expected_is_our_offer: bool',
    "authority permit annotation",
)
s = replace_once(
    s,
    '''        recipient = _exact_text(self.recipient_steam_id, field="recipient_steam_id")
        counterparty = _exact_steam_id(
            self.expected_counterparty_steam_id,
            field="expected_counterparty_steam_id",
        )
        if counterparty == recipient:
            raise CanaryAuthorityError("invalid_expected_counterparty_steam_id")
        if type(self.expected_is_our_offer) is not bool:
            raise CanaryAuthorityError("invalid_expected_is_our_offer")
''',
    '''        recipient = _exact_text(self.recipient_steam_id, field="recipient_steam_id")
        if type(self.expected_is_our_offer) is not bool:
            raise CanaryAuthorityError("invalid_expected_is_our_offer")
        raw_counterparty = self.expected_counterparty_steam_id
        if raw_counterparty is None:
            # Source-native buyer-send authority proves direction before SEND,
            # but counterparty only becomes exact after the resulting Trade Offer.
            if self.expected_is_our_offer is not True:
                raise CanaryAuthorityError("invalid_expected_counterparty_steam_id")
        else:
            counterparty = _exact_steam_id(
                raw_counterparty,
                field="expected_counterparty_steam_id",
            )
            if counterparty == recipient:
                raise CanaryAuthorityError("invalid_expected_counterparty_steam_id")
''',
    "authority permit validation",
)
s = replace_once(
    s,
    '        if target.action not in {"auto_offer_send", "auto_offer_confirm"}:\n            yield\n            return',
    '        if target.action not in {"auto_offer_send", "auto_offer_confirm", "auto_offer_accept"}:\n            yield\n            return',
    "authority owner db barrier",
)
s = replace_once(
    s,
    '''        if target.action == "host_receipt":
            if target.host_db_id != permit.host_db_id or target.assetid is None:
                raise CanaryWriteBlockedError("canary_host_receipt_identity_required")
        elif target.host_db_id is not None or target.assetid is not None:
            raise CanaryWriteBlockedError("canary_write_target_excess_identity")
''',
    '''        if target.action in {"auto_offer_send", "auto_offer_confirm"}:
            if permit.expected_is_our_offer is not True:
                raise CanaryWriteBlockedError("canary_direction_mismatch")
        elif target.action == "auto_offer_accept":
            if (
                permit.expected_is_our_offer is not False
                or permit.expected_counterparty_steam_id is None
            ):
                raise CanaryWriteBlockedError("canary_direction_mismatch")
        if target.action == "host_receipt":
            if target.host_db_id != permit.host_db_id or target.assetid is None:
                raise CanaryWriteBlockedError("canary_host_receipt_identity_required")
        elif target.host_db_id is not None or target.assetid is not None:
            raise CanaryWriteBlockedError("canary_write_target_excess_identity")
''',
    "authority direction gate",
)
write(path, s)


# ---------------------------------------------------------------------------
# coordinator.py: allow a direction-only buyer canary guard. Reconciliation
# remains the source of truth for the post-SEND exact counterparty binding.
# ---------------------------------------------------------------------------
path = "app/auto_offer/coordinator.py"
s = read(path)
s = replace_once(
    s,
    '''def _validate_trade_offer_expectations(
    counterparty_steam_id: object,
    is_our_offer: object,
) -> tuple[str | None, bool | None]:
    if counterparty_steam_id is None and is_our_offer is None:
        return None, None
    if (
        type(counterparty_steam_id) is not str
        or not counterparty_steam_id
        or counterparty_steam_id.strip() != counterparty_steam_id
        or any(ord(character) < 32 for character in counterparty_steam_id)
    ):
        raise ReadOnlyCoordinatorError("invalid_expected_trade_offer_counterparty")
    if type(is_our_offer) is not bool:
        raise ReadOnlyCoordinatorError("invalid_expected_trade_offer_direction")
    return counterparty_steam_id, is_our_offer
''',
    '''def _validate_trade_offer_expectations(
    counterparty_steam_id: object,
    is_our_offer: object,
) -> tuple[str | None, bool | None]:
    if counterparty_steam_id is None:
        if is_our_offer is None:
            return None, None
        if is_our_offer is not True:
            raise ReadOnlyCoordinatorError("invalid_expected_trade_offer_direction")
        return None, True
    if (
        type(counterparty_steam_id) is not str
        or not counterparty_steam_id
        or counterparty_steam_id.strip() != counterparty_steam_id
        or any(ord(character) < 32 for character in counterparty_steam_id)
    ):
        raise ReadOnlyCoordinatorError("invalid_expected_trade_offer_counterparty")
    if type(is_our_offer) is not bool:
        raise ReadOnlyCoordinatorError("invalid_expected_trade_offer_direction")
    return counterparty_steam_id, is_our_offer
''',
    "coordinator expectation validation",
)
s = replace_once(
    s,
    '''        if (
            self._expected_trade_offer_counterparty_steam_id is None
            or self._expected_trade_offer_is_our_offer is None
        ):
            raise ReadOnlyCoordinatorBlockedError("confirmation_identity_guard_required")
''',
    '''        if self._expected_trade_offer_is_our_offer is not True:
            raise ReadOnlyCoordinatorBlockedError("confirmation_identity_guard_required")
''',
    "coordinator confirmation direction guard",
)
write(path, s)


# ---------------------------------------------------------------------------
# host_integration.py: nullable buyer pre-SEND counterparty, owner ACCEPT using
# the existing seller exact-evidence path, and strict one-shot canary SEND from
# an already direction-bound captured Store row.
# ---------------------------------------------------------------------------
path = "app/auto_offer/host_integration.py"
s = read(path)
s = replace_once(
    s,
    '    expected_counterparty_steam_id: str,\n    expected_is_our_offer: bool,',
    '    expected_counterparty_steam_id: str | None,\n    expected_is_our_offer: bool,',
    "host permit annotation",
)
s = replace_once(
    s,
    '''    recipient = _canonical_steam_id(recipient_steam_id)
    counterparty = _canonical_steam_id(expected_counterparty_steam_id)
    if counterparty == recipient:
        raise HostAutoOfferIntegrationError("canary_counterparty_invalid")
    if type(expected_is_our_offer) is not bool:
        raise HostAutoOfferIntegrationError("canary_direction_invalid")
''',
    '''    recipient = _canonical_steam_id(recipient_steam_id)
    if type(expected_is_our_offer) is not bool:
        raise HostAutoOfferIntegrationError("canary_direction_invalid")
    if expected_counterparty_steam_id is None:
        if expected_is_our_offer is not True:
            raise HostAutoOfferIntegrationError("canary_counterparty_invalid")
        counterparty = None
    else:
        counterparty = _canonical_steam_id(expected_counterparty_steam_id)
        if counterparty == recipient:
            raise HostAutoOfferIntegrationError("canary_counterparty_invalid")
''',
    "host permit validation",
)
s = replace_once(
    s,
    '''        accept_transport = None
        if canary_permit is None:
            accept_transport = SteamIncomingOfferAcceptTransport(
                cookie_string,
                session=session,
            )
''',
    '''        accept_transport = SteamIncomingOfferAcceptTransport(
            cookie_string,
            session=session,
        )
''',
    "host canary accept transport",
)
s = replace_once(
    s,
    '''            expected_trade_offer_is_our_offer=(
                None if canary_permit is None else canary_permit.expected_is_our_offer
            ),
''',
    '''            expected_trade_offer_is_our_offer=(
                None if canary_permit is None else canary_permit.expected_is_our_offer
            ),
''',
    "host expected direction unchanged marker",
)
# Confirmation may use the durable post-SEND counterparty when the buyer permit
# intentionally had none before SEND.
s = replace_once(
    s,
    '''        evidence = platform_result.evidence
        if platform_result.status is not PlatformResultStatus.SUCCESS:
            return AutoOfferResult.WAITING, current, False
        if (
            type(evidence) is not SteamTradeOfferEvidence
            or evidence.steam_tradeoffer_id != current.snapshot.steam_tradeoffer_id
            or evidence.account_steam_id != permit.recipient_steam_id
            or evidence.counterparty_steam_id
            != permit.expected_counterparty_steam_id
            or evidence.is_our_offer is not permit.expected_is_our_offer
            or permit.expected_is_our_offer is not True
            or evidence.items_to_give != ()
            or evidence.lifecycle
            is not SteamTradeOfferLifecycle.CREATED_NEEDS_CONFIRMATION
        ):
''',
    '''        evidence = platform_result.evidence
        if platform_result.status is not PlatformResultStatus.SUCCESS:
            return AutoOfferResult.WAITING, current, False
        expected_counterparty = (
            permit.expected_counterparty_steam_id
            or current.snapshot.counterparty_steam_id
        )
        if (
            expected_counterparty is None
            or type(evidence) is not SteamTradeOfferEvidence
            or evidence.steam_tradeoffer_id != current.snapshot.steam_tradeoffer_id
            or evidence.account_steam_id != permit.recipient_steam_id
            or evidence.counterparty_steam_id != expected_counterparty
            or evidence.is_our_offer is not permit.expected_is_our_offer
            or permit.expected_is_our_offer is not True
            or evidence.items_to_give != ()
            or evidence.lifecycle
            is not SteamTradeOfferLifecycle.CREATED_NEEDS_CONFIRMATION
        ):
''',
    "host confirmation late counterparty",
)

anchor = '    def _recover_canary_persisted_delivery(\n'
insert = r'''    def _step_canary_send_once(
        self,
        host_purchase: Mapping[str, object],
        current: StoredDelivery,
    ) -> AutoOfferResult:
        """Run the canary's only first SEND from fresh exact BUFF authority."""

        permit = self._canary_permit
        snapshot = current.snapshot
        db_id = _exact_db_id(host_purchase.get("_db_id"))
        if (
            permit is None
            or permit.expected_is_our_offer is not True
            or permit.expected_counterparty_steam_id is not None
            or db_id != permit.host_db_id
            or host_purchase.get("buff_order_id") != permit.buff_order_id
            or host_purchase.get("pending_receipt") is not True
            or host_purchase.get("assetid") not in (None, "")
            or snapshot.delivery_mode is not DeliveryMode.BUYER_SENDS_OFFER
            or snapshot.delivery_status is not DeliveryStatus.AWAITING_OFFER
            or snapshot.offer_attempted_at is not None
            or snapshot.offer_sent_at is not None
            or snapshot.steam_tradeoffer_id is not None
            or snapshot.counterparty_steam_id is not None
        ):
            raise HostAutoOfferIntegrationError("canary_send_target_invalid")
        refreshed = self._bridge.get_by_purchase_id(snapshot.purchase_id)
        if refreshed != current:
            raise HostAutoOfferIntegrationError("canary_send_store_changed")
        proof = self._bridge.read_send_authority(refreshed)
        if proof is None:
            return AutoOfferResult.WAITING
        send_result = self._bridge.send_offer_with_authority(refreshed, proof)
        before = getattr(send_result, "before", None)
        attempted = getattr(send_result, "attempted", None)
        after = getattr(send_result, "after", None)
        platform_result = getattr(send_result, "platform_result", None)
        if (
            before != refreshed
            or type(attempted) is not StoredDelivery
            or type(after) is not StoredDelivery
            or type(platform_result) is not PlatformResult
            or attempted.revision != refreshed.revision + 1
            or after != attempted
            or attempted.snapshot.delivery_status is not DeliveryStatus.OFFER_ATTEMPTED
            or attempted.snapshot.offer_attempted_at is None
            or attempted.snapshot.steam_tradeoffer_id is not None
            or attempted.snapshot.counterparty_steam_id is not None
            or platform_result.request.capability is not PlatformCapability.SEND_OFFER
            or platform_result.request.revision != attempted.revision
            or platform_result.request.purchase_id != snapshot.purchase_id
            or platform_result.request.buff_order_id != snapshot.buff_order_id
            or platform_result.request.account_id != snapshot.account_id
            or platform_result.request.recipient_steam_id != snapshot.recipient_steam_id
        ):
            raise HostAutoOfferIntegrationError("canary_send_result_invalid")
        return AutoOfferResult.WAITING

    def _step_canary_accept_once(
        self,
        host_purchase: Mapping[str, object],
        current: StoredDelivery,
    ) -> AutoOfferResult:
        """Use the existing exact seller evidence for the canary's only ACCEPT."""

        permit = self._canary_permit
        snapshot = current.snapshot
        db_id = _exact_db_id(host_purchase.get("_db_id"))
        goods_id = _exact_goods_id(host_purchase.get("goods_id"))
        if (
            permit is None
            or permit.expected_is_our_offer is not False
            or permit.expected_counterparty_steam_id is None
            or permit.expected_counterparty_steam_id != snapshot.counterparty_steam_id
            or db_id != permit.host_db_id
            or goods_id is None
            or host_purchase.get("buff_order_id") != permit.buff_order_id
            or host_purchase.get("pending_receipt") is not True
            or host_purchase.get("assetid") not in (None, "")
            or snapshot.delivery_mode is not DeliveryMode.SELLER_SENDS_OFFER
            or snapshot.delivery_status is not DeliveryStatus.OFFER_CONFIRMED
            or snapshot.steam_tradeoffer_id is None
            or snapshot.counterparty_steam_id is None
            or snapshot.offer_attempted_at is not None
            or snapshot.offer_sent_at is not None
        ):
            raise HostAutoOfferIntegrationError("canary_accept_target_invalid")
        refreshed = self._bridge.get_by_purchase_id(snapshot.purchase_id)
        if refreshed != current:
            raise HostAutoOfferIntegrationError("canary_accept_store_changed")
        authority_result = self._bridge.read_seller_accept_authority(
            refreshed,
            goods_id,
        )
        if (
            type(authority_result) is not SellerAcceptAuthorityReadResult
            or authority_result.before != refreshed
            or authority_result.host_goods_id != goods_id
        ):
            raise HostAutoOfferIntegrationError("canary_accept_authority_invalid")
        steam_result = authority_result.steam_result
        proof = authority_result.proof
        if steam_result is None:
            if proof is not None:
                raise HostAutoOfferIntegrationError("canary_accept_authority_invalid")
            return AutoOfferResult.WAITING
        decision_result = steam_result.decision.result
        if decision_result is AutoOfferResult.BLOCKED:
            if steam_result.persisted or steam_result.after != refreshed or proof is not None:
                raise HostAutoOfferIntegrationError("canary_accept_authority_invalid")
            return AutoOfferResult.BLOCKED
        if steam_result.persisted:
            after = steam_result.after
            if (
                proof is not None
                or decision_result is not AutoOfferResult.WAITING
                or after.revision != refreshed.revision + 1
                or after.snapshot.delivery_status not in {
                    DeliveryStatus.AWAITING_INVENTORY,
                    DeliveryStatus.OFFER_TERMINATED,
                }
                or after.snapshot.steam_tradeoffer_id != refreshed.snapshot.steam_tradeoffer_id
                or after.snapshot.counterparty_steam_id != refreshed.snapshot.counterparty_steam_id
            ):
                raise HostAutoOfferIntegrationError("canary_accept_authority_invalid")
            return AutoOfferResult.WAITING
        if steam_result.after != refreshed or decision_result is not AutoOfferResult.WAITING:
            raise HostAutoOfferIntegrationError("canary_accept_authority_invalid")
        if proof is None:
            return AutoOfferResult.WAITING
        accept_result = self._bridge.accept_offer_with_authority(refreshed, proof)
        if type(accept_result) is not AcceptOfferStepResult:
            raise HostAutoOfferIntegrationError("canary_accept_result_invalid")
        attempted = accept_result.attempted
        after = accept_result.after
        platform_result = accept_result.platform_result
        request = platform_result.request
        if (
            accept_result.before != refreshed
            or attempted.revision != refreshed.revision + 1
            or attempted.snapshot.delivery_status is not DeliveryStatus.OFFER_ACCEPT_ATTEMPTED
            or request.capability is not PlatformCapability.ACCEPT_OFFER
            or request.purchase_id != snapshot.purchase_id
            or request.buff_order_id != snapshot.buff_order_id
            or request.account_id != snapshot.account_id
            or request.recipient_steam_id != snapshot.recipient_steam_id
            or request.steam_tradeoffer_id != snapshot.steam_tradeoffer_id
            or request.counterparty_steam_id != snapshot.counterparty_steam_id
        ):
            raise HostAutoOfferIntegrationError("canary_accept_result_invalid")
        if (
            platform_result.status is PlatformResultStatus.FAILURE
            and platform_result.detail == "write_preflight_failed"
        ):
            if after != attempted:
                raise HostAutoOfferIntegrationError("canary_accept_result_invalid")
            return AutoOfferResult.WAITING
        if (
            after.revision != attempted.revision + 1
            or after.snapshot.delivery_status is not DeliveryStatus.RESULT_UNKNOWN
            or after.snapshot.delivery_error != "write_result_unknown"
        ):
            raise HostAutoOfferIntegrationError("canary_accept_result_invalid")
        return AutoOfferResult.RESULT_UNKNOWN

'''
if s.count(anchor) != 1:
    raise RuntimeError("host canary recovery anchor")
s = s.replace(anchor, insert + anchor, 1)
s = replace_once(
    s,
    '''    def _recover_canary_persisted_delivery(
        self,
        delivery: StoredDelivery,
    ) -> tuple[AutoOfferResult, StoredDelivery]:
        current = delivery
        if (
            self.is_canary
            and current.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN
        ):
            return AutoOfferResult.RESULT_UNKNOWN, current
        for _step_index in range(_MAX_CANARY_RECOVERY_STEPS_PER_DELIVERY):
            policy = _persisted_recovery_policy(current)
''',
    '''    def _recover_canary_persisted_delivery(
        self,
        host_purchase: Mapping[str, object],
        delivery: StoredDelivery,
    ) -> tuple[AutoOfferResult, StoredDelivery]:
        current = delivery
        if (
            self.is_canary
            and current.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN
        ):
            return AutoOfferResult.RESULT_UNKNOWN, current
        for _step_index in range(_MAX_CANARY_RECOVERY_STEPS_PER_DELIVERY):
            if (
                current.snapshot.delivery_mode is DeliveryMode.BUYER_SENDS_OFFER
                and current.snapshot.delivery_status is DeliveryStatus.AWAITING_OFFER
            ):
                result = self._step_canary_send_once(host_purchase, current)
                refreshed = self._bridge.get_by_purchase_id(current.snapshot.purchase_id)
                if type(refreshed) is not StoredDelivery:
                    raise HostAutoOfferIntegrationError("canary_send_store_missing")
                return result, refreshed
            if (
                current.snapshot.delivery_mode is DeliveryMode.SELLER_SENDS_OFFER
                and current.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMED
            ):
                result = self._step_canary_accept_once(host_purchase, current)
                refreshed = self._bridge.get_by_purchase_id(current.snapshot.purchase_id)
                if type(refreshed) is not StoredDelivery:
                    raise HostAutoOfferIntegrationError("canary_accept_store_missing")
                return result, refreshed
            policy = _persisted_recovery_policy(current)
''',
    "host canary recovery signature",
)
s = replace_once(
    s,
    '            result, current = self._recover_canary_persisted_delivery(stored)',
    '            result, current = self._recover_canary_persisted_delivery(\n                host_pending[target],\n                stored,\n            )',
    "host canary recovery call",
)
write(path, s)


# ---------------------------------------------------------------------------
# canary_takeover.py: PREPARED is target-agnostic; capture first, then run only
# the existing normal read-only direction step until exact direction is durable,
# then create/arm the owner permit. No delivery write occurs in the binding tick.
# ---------------------------------------------------------------------------
path = "app/auto_offer/canary_takeover.py"
s = read(path)
s = replace_once(
    s,
    '''        self._permit = None
        self._active_integration = None
''',
    '''        self._permit = None
        self._active_integration = None
        self._captured_integration = None
        self._build_canary_integration = None
''',
    "takeover init retained integration",
)
s = replace_once(
    s,
    '''    def prepare(
        self,
        *,
        expected_counterparty_steam_id: str,
        expected_is_our_offer: bool,
        host_purchases: Sequence[Mapping[str, object]] | None = None,
        store_rows: Sequence[object] | None = None,
    ) -> CanaryTakeoverStatus:
        counterparty = _canonical_steam_id(expected_counterparty_steam_id)
        if type(expected_is_our_offer) is not bool:
            raise CanaryTakeoverError("invalid_expected_is_our_offer")
''',
    '''    def prepare(
        self,
        *,
        host_purchases: Sequence[Mapping[str, object]] | None = None,
        store_rows: Sequence[object] | None = None,
    ) -> CanaryTakeoverStatus:
''',
    "takeover prepare signature",
)
s = replace_once(
    s,
    '''            self._expected_counterparty = counterparty
            self._expected_is_our_offer = expected_is_our_offer
            self._target = {}
            self._reason = None
            self._permit = None
            self._active_integration = None
            self._phase = CanaryTakeoverPhase.PREPARED
''',
    '''            self._expected_counterparty = None
            self._expected_is_our_offer = None
            self._target = {}
            self._reason = None
            self._permit = None
            self._active_integration = None
            self._captured_integration = None
            self._build_canary_integration = None
            self._phase = CanaryTakeoverPhase.PREPARED
''',
    "takeover prepare state",
)
s = replace_once(
    s,
    '''        self._permit = None
        self._active_integration = None

    def capture_committed_purchases(
''',
    '''        self._permit = None
        self._active_integration = None
        self._captured_integration = None
        self._build_canary_integration = None

    def capture_committed_purchases(
''',
    "takeover clear state",
)

start = '    def capture_committed_purchases(\n'
end = '    def run_owner_tick(self, host_purchases: object):\n'
replacement = r'''    def _abort_locked(self, reason: str) -> None:
        normal = self._captured_integration
        self._captured_integration = None
        self._build_canary_integration = None
        self._active_integration = None
        self._permit = None
        self._reason = reason
        self._phase = CanaryTakeoverPhase.ABORTED
        if normal is not None:
            close = getattr(normal, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def _validate_captured_host_locked(self, host_purchases: object) -> list[Mapping[str, object]]:
        order_id = self._target.get("buff_order_id")
        db_id = self._target.get("host_db_id")
        if not isinstance(host_purchases, Sequence) or isinstance(host_purchases, (str, bytes)):
            raise CanaryTakeoverError("canary_host_snapshot_invalid")
        pending = [
            item
            for item in host_purchases
            if isinstance(item, Mapping)
            and item.get("pending_receipt") is True
            and item.get("assetid") in (None, "")
        ]
        matches = [
            item
            for item in pending
            if item.get("buff_order_id") == order_id and item.get("_db_id") == db_id
        ]
        if len(pending) != 1 or len(matches) != 1:
            raise CanaryTakeoverError("canary_host_target_not_exclusive")
        return list(host_purchases)

    def _activate_if_direction_bound_locked(
        self,
        host_purchases: list[Mapping[str, object]],
        *,
        run_direction_read: bool,
    ) -> bool:
        normal = self._captured_integration
        builder = self._build_canary_integration
        if normal is None or not callable(builder):
            raise CanaryTakeoverError("canary_capture_context_missing")
        order_id = self._target.get("buff_order_id")
        purchase_id = self._target.get("purchase_id")
        db_id = self._target.get("host_db_id")
        account_id = self._target.get("account_id")
        recipient = self._target.get("recipient_steam_id")
        if (
            type(order_id) is not str
            or type(purchase_id) is not str
            or type(db_id) is not int
            or type(account_id) is not str
            or type(recipient) is not str
        ):
            raise CanaryTakeoverError("canary_target_invalid")

        stored = normal.get_by_purchase_id(purchase_id)
        if type(stored) is not StoredDelivery:
            raise CanaryTakeoverError("canary_store_target_missing")
        if stored.snapshot.delivery_status is DeliveryStatus.PENDING_DIRECTION and run_direction_read:
            outcome = normal.run_delivery_tick(host_purchases, cursor=None)
            result = getattr(outcome, "result", None)
            if result in {AutoOfferResult.BLOCKED, AutoOfferResult.RESULT_UNKNOWN}:
                raise CanaryTakeoverError("canary_direction_read_blocked")
            if result not in {AutoOfferResult.WAITING, AutoOfferResult.COMPLETE}:
                raise CanaryTakeoverError("canary_direction_read_invalid")
            stored = normal.get_by_purchase_id(purchase_id)
            if type(stored) is not StoredDelivery:
                raise CanaryTakeoverError("canary_store_target_missing")

        snapshot = stored.snapshot
        if snapshot.delivery_status is DeliveryStatus.PENDING_DIRECTION:
            return False
        if snapshot.delivery_status is not DeliveryStatus.AWAITING_OFFER:
            raise CanaryTakeoverError("canary_direction_state_invalid")

        from .contracts import DeliveryMode
        if snapshot.delivery_mode is DeliveryMode.BUYER_SENDS_OFFER:
            if snapshot.counterparty_steam_id is not None:
                raise CanaryTakeoverError("canary_buyer_counterparty_premature")
            counterparty = None
            is_our_offer = True
        elif snapshot.delivery_mode is DeliveryMode.SELLER_SENDS_OFFER:
            counterparty = _canonical_steam_id(snapshot.counterparty_steam_id)
            if counterparty == _canonical_steam_id(recipient):
                raise CanaryTakeoverError("canary_counterparty_invalid")
            is_our_offer = False
        else:
            raise CanaryTakeoverError("canary_direction_state_invalid")

        recoverable = tuple(normal.list_recoverable())
        if recoverable != (stored,):
            raise CanaryTakeoverError("canary_store_target_not_exclusive")
        if self._checkout_provider() is not None:
            raise CanaryTakeoverError("canary_checkout_unresolved")

        from .host_integration import preflight_canary_permit
        permit = preflight_canary_permit(
            host_purchases=host_purchases,
            unresolved_checkout=None,
            recoverable_deliveries=recoverable,
            target_stored=stored,
            target_db_id=db_id,
            target_buff_order_id=order_id,
            account_id=account_id,
            recipient_steam_id=recipient,
            expected_counterparty_steam_id=counterparty,
            expected_is_our_offer=is_our_offer,
            permit_id=uuid.uuid4().hex,
            owner_nonce=uuid.uuid4().hex,
            created_at=float(self._clock()),
        )
        self._expected_counterparty = counterparty
        self._expected_is_our_offer = is_our_offer
        close = getattr(normal, "close", None)
        if callable(close):
            close()
        self._captured_integration = None
        active = builder(permit)
        if active is None:
            raise CanaryTakeoverError("canary_owner_integration_missing")
        self._permit = permit
        self._active_integration = active
        self._build_canary_integration = None
        self._phase = CanaryTakeoverPhase.OWNER_ACTIVE
        return True

    def capture_committed_purchases(
        self,
        purchases: Sequence[Mapping[str, object]],
        *,
        normal_integration,
        build_canary_integration: Callable[[object], object],
        reconcile_checkout: Callable[[], object] | None = None,
    ) -> CanaryTakeoverStatus:
        """Capture one committed target, then late-bind its exact direction."""

        if not isinstance(purchases, Sequence) or isinstance(purchases, (str, bytes)):
            raise CanaryTakeoverError("canary_commit_snapshot_invalid")
        with self._lock:
            if self._phase is not CanaryTakeoverPhase.PREPARED:
                raise CanaryTakeoverError("canary_takeover_not_prepared")
            self._phase = CanaryTakeoverPhase.TARGET_CAPTURED
            try:
                if len(purchases) != 1:
                    raise CanaryTakeoverError("canary_multiple_committed_purchases")
                committed = purchases[0]
                if not isinstance(committed, Mapping):
                    raise CanaryTakeoverError("canary_commit_snapshot_invalid")
                order_id = committed.get("buff_order_id")
                if type(order_id) is not str or not order_id or order_id.strip() != order_id:
                    raise CanaryTakeoverError("canary_target_invalid")
                host_purchases = list(self._host_purchases_provider())
                pending = [
                    item
                    for item in host_purchases
                    if isinstance(item, Mapping)
                    and item.get("pending_receipt") is True
                    and item.get("assetid") in (None, "")
                ]
                matches = [item for item in pending if item.get("buff_order_id") == order_id]
                if len(pending) != 1 or len(matches) != 1:
                    raise CanaryTakeoverError("canary_host_target_not_exclusive")
                target = matches[0]
                db_id = target.get("_db_id")
                if type(db_id) is not int or db_id <= 0:
                    raise CanaryTakeoverError("canary_host_target_invalid")
                unresolved = self._checkout_provider()
                if unresolved is not None and reconcile_checkout is not None:
                    reconcile_checkout()
                    unresolved = self._checkout_provider()
                if unresolved is not None:
                    raise CanaryTakeoverError("canary_checkout_unresolved")
                account_id = normal_integration.account_id
                recipient = normal_integration.recipient_steam_id
                self._target = {
                    "host_db_id": db_id,
                    "buff_order_id": order_id,
                    "purchase_id": f"buff:{order_id}",
                    "account_id": account_id,
                    "recipient_steam_id": recipient,
                }
                self._captured_integration = normal_integration
                self._build_canary_integration = build_canary_integration
                self._activate_if_direction_bound_locked(
                    host_purchases,
                    run_direction_read=True,
                )
                return self.status()
            except CanaryTakeoverError as exc:
                self._abort_locked(str(exc) or type(exc).__name__)
                raise
            except Exception as exc:
                self._abort_locked(type(exc).__name__)
                raise CanaryTakeoverError("canary_takeover_activation_failed") from exc

    def run_capture_binding_tick(self, host_purchases: object):
        """Run one existing production read step while the captured target is fenced."""

        from .host_integration import DeliveryTickOutcome
        with self._lock:
            if self._phase is not CanaryTakeoverPhase.TARGET_CAPTURED:
                return DeliveryTickOutcome(AutoOfferResult.BLOCKED, None, ())
            order_id = self._target.get("buff_order_id")
            try:
                current_host = self._validate_captured_host_locked(host_purchases)
                self._activate_if_direction_bound_locked(
                    current_host,
                    run_direction_read=True,
                )
                return DeliveryTickOutcome(
                    AutoOfferResult.WAITING,
                    order_id if isinstance(order_id, str) else None,
                    (order_id,) if isinstance(order_id, str) else (),
                )
            except CanaryTakeoverError as exc:
                self._abort_locked(str(exc) or type(exc).__name__)
            except Exception as exc:
                self._abort_locked(type(exc).__name__)
            return DeliveryTickOutcome(
                AutoOfferResult.BLOCKED,
                order_id if isinstance(order_id, str) else None,
                (order_id,) if isinstance(order_id, str) else (),
            )

'''
s = replace_between(s, start, end, replacement, "takeover capture block")
write(path, s)


# ---------------------------------------------------------------------------
# Prepare route has no pre-purchase target fields.
# ---------------------------------------------------------------------------
path = "app/routes/pipeline.py"
s = read(path)
s = replace_once(
    s,
    'from pydantic import BaseModel, StrictBool, StrictStr',
    'from pydantic import BaseModel',
    "route pydantic import",
)
s = replace_once(
    s,
    '''

class CanaryTakeoverPrepareBody(BaseModel):
    expected_counterparty_steam_id: StrictStr
    expected_is_our_offer: StrictBool
''',
    '',
    "route prepare model",
)
s = replace_once(
    s,
    '''@router.post("/api/pipeline/canary_takeover/prepare")
def api_canary_takeover_prepare(body: CanaryTakeoverPrepareBody):
''',
    '''@router.post("/api/pipeline/canary_takeover/prepare")
def api_canary_takeover_prepare():
''',
    "route prepare signature",
)
s = replace_once(
    s,
    '''        prepared = get_canary_takeover().prepare(
            expected_counterparty_steam_id=body.expected_counterparty_steam_id,
            expected_is_our_offer=body.expected_is_our_offer,
        )
''',
    '''        prepared = get_canary_takeover().prepare()
''',
    "route prepare call",
)
write(path, s)


# ---------------------------------------------------------------------------
# Existing Host receive worker is the sole cadence for TARGET_CAPTURED late
# binding as well as OWNER_ACTIVE delivery.
# ---------------------------------------------------------------------------
path = "app/services/workers.py"
s = read(path)
s = replace_once(
    s,
    '''    takeover = get_canary_takeover()
    if takeover.owner_active:
        outcome = takeover.run_owner_tick(purchases)
        if type(outcome) is not DeliveryTickOutcome:
            raise RuntimeError("canary_delivery_tick_outcome_invalid")
        return outcome
    if takeover.receive_blocked:
''',
    '''    takeover = get_canary_takeover()
    if takeover.owner_active:
        outcome = takeover.run_owner_tick(purchases)
        if type(outcome) is not DeliveryTickOutcome:
            raise RuntimeError("canary_delivery_tick_outcome_invalid")
        return outcome
    from app.auto_offer.canary_takeover import CanaryTakeoverPhase
    if takeover.phase is CanaryTakeoverPhase.TARGET_CAPTURED:
        outcome = takeover.run_capture_binding_tick(purchases)
        if type(outcome) is not DeliveryTickOutcome:
            raise RuntimeError("canary_binding_tick_outcome_invalid")
        return outcome
    if takeover.receive_blocked:
''',
    "worker helper target captured",
)
s = replace_once(
    s,
    '''                    takeover = get_canary_takeover()
                    if takeover.owner_active:
                        outcome = _run_auto_offer_delivery_tick(
''',
    '''                    takeover = get_canary_takeover()
                    from app.auto_offer.canary_takeover import CanaryTakeoverPhase
                    if (
                        takeover.owner_active
                        or takeover.phase is CanaryTakeoverPhase.TARGET_CAPTURED
                    ):
                        outcome = _run_auto_offer_delivery_tick(
''',
    "receive worker target captured",
)
write(path, s)


# ---------------------------------------------------------------------------
# Adapt existing takeover tests and add focused late-binding assertions.
# ---------------------------------------------------------------------------
path = "tests/test_auto_offer_canary_takeover.py"
s = read(path)
s = replace_once(
    s,
    '''    def list_recoverable(self):
        return self.recoverable

    def close(self):
        self.closed += 1
''',
    '''    def list_recoverable(self):
        return self.recoverable

    def run_delivery_tick(self, purchases, *, cursor=None):
        if (
            self.stored is not None
            and self.stored.snapshot.delivery_status is DeliveryStatus.PENDING_DIRECTION
        ):
            self.stored = _stored(DeliveryStatus.AWAITING_OFFER)
            self.recoverable = (self.stored,)
        return DeliveryTickOutcome(AutoOfferResult.WAITING, ORDER_ID, (ORDER_ID,))

    def close(self):
        self.closed += 1
''',
    "takeover test normal integration",
)
s = replace_once(
    s,
    '''        controller.prepare(
            expected_counterparty_steam_id=COUNTERPARTY,
            expected_is_our_offer=True,
        )
''',
    '''        controller.prepare()
''',
    "takeover test prepare helper",
)
s += r'''


def test_prepare_has_no_target_specific_identity_or_direction():
    controller = _controller([])
    _prepare(controller)
    status = controller.status()
    assert status.phase is CanaryTakeoverPhase.PREPARED
    assert status.expected_counterparty_steam_id is None
    assert status.expected_is_our_offer is None


def test_capture_waits_fenced_until_existing_direction_read_proves_target():
    host_rows: list[dict] = []
    controller = _controller(host_rows)
    _prepare(controller)
    host_rows.append(_host_row())

    class WaitingNormal(_NormalIntegration):
        def run_delivery_tick(self, purchases, *, cursor=None):
            return DeliveryTickOutcome(AutoOfferResult.WAITING, ORDER_ID, (ORDER_ID,))

    normal = WaitingNormal(_stored(), (_stored(),))
    built = []
    status = controller.capture_committed_purchases(
        ({"buff_order_id": ORDER_ID},),
        normal_integration=normal,
        build_canary_integration=lambda permit: (built.append(permit) or _OwnerIntegration()),
    )
    assert status.phase is CanaryTakeoverPhase.TARGET_CAPTURED
    assert controller.purchase_blocked is True
    assert built == []

    normal.stored = _stored(DeliveryStatus.AWAITING_OFFER)
    normal.recoverable = (normal.stored,)
    outcome = controller.run_capture_binding_tick(host_rows)
    assert outcome.result is AutoOfferResult.WAITING
    assert controller.phase is CanaryTakeoverPhase.OWNER_ACTIVE
    assert len(built) == 1
    assert built[0].expected_counterparty_steam_id is None
    assert built[0].expected_is_our_offer is True
'''
write(path, s)


# Focused contract tests: buyer direction may be bound before counterparty, while
# seller direction never may.
new_test = r'''from __future__ import annotations

import pytest

from app.auto_offer.canary_authority import CanaryAuthorityError, CanaryPermit
from app.auto_offer.coordinator import (
    ReadOnlyCoordinatorError,
    _validate_trade_offer_expectations,
)
from app.auto_offer.host_integration import (
    HostAutoOfferIntegrationError,
    preflight_canary_permit,
)
from app.auto_offer.contracts import DeliveryMode, DeliverySnapshot, DeliveryStatus
from app.auto_offer.store import StoredDelivery


ORDER = "late-bind-order"
ACCOUNT = "account"
RECIPIENT = "76561198000000001"
SELLER = "76561198000000002"


def _permit(counterparty, direction):
    return CanaryPermit(
        permit_id="permit",
        owner_nonce="nonce",
        host_db_id=1,
        buff_order_id=ORDER,
        purchase_id=f"buff:{ORDER}",
        account_id=ACCOUNT,
        recipient_steam_id=RECIPIENT,
        expected_counterparty_steam_id=counterparty,
        expected_is_our_offer=direction,
        expected_host_order_ids=(ORDER,),
        expected_store_present=True,
        expected_store_revision=2,
        expected_store_status=DeliveryStatus.AWAITING_OFFER.value,
        expected_store_tradeoffer_id=None,
        created_at=1.0,
    )


def test_buyer_permit_allows_direction_only_before_send():
    permit = _permit(None, True)
    assert permit.expected_counterparty_steam_id is None
    assert permit.expected_is_our_offer is True
    assert _validate_trade_offer_expectations(None, True) == (None, True)


def test_seller_permit_requires_exact_counterparty_before_accept():
    with pytest.raises(CanaryAuthorityError):
        _permit(None, False)
    with pytest.raises(ReadOnlyCoordinatorError):
        _validate_trade_offer_expectations(None, False)
    assert _permit(SELLER, False).expected_counterparty_steam_id == SELLER


def test_preflight_accepts_late_bound_buyer_but_rejects_unbound_seller():
    stored = StoredDelivery(
        DeliverySnapshot(
            purchase_id=f"buff:{ORDER}",
            buff_order_id=ORDER,
            account_id=ACCOUNT,
            recipient_steam_id=RECIPIENT,
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
        2,
    )
    host = [{"_db_id": 1, "buff_order_id": ORDER, "pending_receipt": True, "assetid": None}]
    permit = preflight_canary_permit(
        host_purchases=host,
        unresolved_checkout=None,
        recoverable_deliveries=(stored,),
        target_stored=stored,
        target_db_id=1,
        target_buff_order_id=ORDER,
        account_id=ACCOUNT,
        recipient_steam_id=RECIPIENT,
        expected_counterparty_steam_id=None,
        expected_is_our_offer=True,
        permit_id="permit-2",
        owner_nonce="nonce-2",
        created_at=2.0,
    )
    assert permit.expected_counterparty_steam_id is None
    with pytest.raises(HostAutoOfferIntegrationError):
        preflight_canary_permit(
            host_purchases=host,
            unresolved_checkout=None,
            recoverable_deliveries=(stored,),
            target_stored=stored,
            target_db_id=1,
            target_buff_order_id=ORDER,
            account_id=ACCOUNT,
            recipient_steam_id=RECIPIENT,
            expected_counterparty_steam_id=None,
            expected_is_our_offer=False,
            permit_id="permit-3",
            owner_nonce="nonce-3",
            created_at=3.0,
        )
'''
write("tests/test_auto_offer_canary_late_binding.py", new_test)

print("TASK098 late-binding patch applied")
