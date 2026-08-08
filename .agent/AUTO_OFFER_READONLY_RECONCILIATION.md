# Auto Offer Read-Only Evidence Reconciliation

## Architecture

This module belongs to `native-auto-offer-module-v1` and is a pure planner
between stored delivery state and the typed read-only platform evidence
contract.

## Purpose and boundary

`plan_read_evidence_transition` accepts one `StoredDelivery` and one
`PlatformResult`, then returns an immutable `ReconciliationDecision`. The
decision may contain a validated proposed `DeliverySnapshot`, but no decision
is persisted and no state transition is executed.

The planner does not own or call `AutoOfferStore`, does not call
`AutoOfferStore.advance`, and has no network, platform, Pipeline, worker,
Purchase Flow, or runtime-registration integration.

## Identity and revision

The request must exactly match `purchase_id`, `buff_order_id`, `account_id`,
`recipient_steam_id`, and the stored integer `revision`. Any mismatch is
`BLOCKED` with `identity_mismatch`. Names, goods IDs, market names, timing,
ordering, and approximate IDs are never used for binding.

## Allowed transitions

- `PENDING_DIRECTION` plus exact seller-direction evidence proposes
  `AWAITING_OFFER` with `SELLER_SENDS_OFFER`.
- `AWAITING_OFFER` in seller mode plus exact `OfferStateEvidence` proposes
  `OFFER_RECEIVED` and preserves the exact Steam trade-offer ID.
- Buyer-side first-send planning is blocked with `write_capability_required`.
- `RESULT_UNKNOWN` never plans a resend or `OFFER_ATTEMPTED`.
- Terminal states never produce a target.

Every proposed target is validated with the existing snapshot and transition
validators, and all delivery identity fields remain unchanged.

## Inventory evidence

`InventoryStateEvidence` proves only that an exact recipient inventory snapshot
was readable. It is not proof that a Purchase was received. The planner never
selects an asset, sets `received_at`, clears `pending_receipt`, or proposes
`RECEIVED` from inventory evidence.

## Result mapping

Read `RESULT_UNKNOWN` and `TIMEOUT` produce waiting, retryable decisions for a
future separate read check. `FAILURE`, `MALFORMED`, `UNSUPPORTED`, and identity
mismatch are blocked. `retryable` does not implement retry loops, sleeping,
scheduling, or threads.

## Future boundary

Actual Store persistence, Purchase-specific inventory reconciliation, buyer
offer writes, platform writes, Pipeline integration, and runtime execution are
separate future work and are not part of this planner.
