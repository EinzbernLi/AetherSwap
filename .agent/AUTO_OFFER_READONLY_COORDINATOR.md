# Auto Offer Read-Only Delivery Step Coordinator

## Architecture and purpose

This coordinator belongs to `native-auto-offer-module-v1`. “Read-only” means
that platform capabilities remain read-only. The coordinator combines the
persisted delivery contract, the typed platform adapter boundary, and the
TASK-011 reconciliation planner into one synchronous step.

The only permitted write is an internal `AutoOfferStore.advance()` after the
planner has returned a validated target. No platform write is performed.

## One-step contract

Each `step` performs at most one persisted-current read, one exact read
adapter call, one reconciliation plan, and one planner-approved CAS advance.
It never loops into a second delivery state or retries a platform operation.

The exact flow is:

`StoredDelivery -> persisted-current gate -> read routing -> exact
PlatformRequest -> one adapter.execute -> TASK-011 planner -> optional one
CAS advance -> frozen ReadOnlyStepResult`.

## Persisted-current gate

Before routing or platform access, the supplied `StoredDelivery` must exactly
equal `get_by_purchase_id(purchase_id)`, including snapshot, identity, and
revision. A missing row or mismatch is a conflict and causes zero adapter and
zero advance calls.

## Routing and request binding

Only these states have a read step:

- `PENDING_DIRECTION` -> `READ_DELIVERY_DIRECTION`;
- seller-mode `AWAITING_OFFER` -> `READ_OFFER_STATE`;
- `AWAITING_INVENTORY` -> `READ_INVENTORY_STATE`.

Buyer-mode first send and all other states fail closed without an adapter call.
The `PlatformRequest` copies `purchase_id`, `buff_order_id`, `account_id`,
`recipient_steam_id`, and `revision` exactly from the current delivery, with
only the routed read capability and configured timeout added.

## Adapter boundary

The registry cannot contain `SEND_OFFER`, and every registered adapter must
declare its mapped capability. A missing adapter becomes normalized
`UNSUPPORTED`. Invalid returned types, forged requests, forged evidence, and
adapter exceptions become bounded normalized results without raw exception
text, credentials, sessions, tokens, or payloads.

There is no fallback adapter, retry loop, sleep, polling, thread, or background
worker. At most one adapter call occurs per step.

## Planner and persistence authority

The coordinator always delegates evidence interpretation and transition choice
to `plan_read_evidence_transition`. It does not duplicate the TASK-011 state
machine.

When the decision has no target, the result is not persisted and `after ==
before`. When a target exists, exactly one `AutoOfferStore.advance()` CAS call
is allowed; a successful result has `after.snapshot == target` and increments
revision by one. A stale write becomes a conflict with no retry.

## Inventory and buyer boundaries

Inventory evidence is not receipt proof. A readable inventory snapshot never
selects an asset, marks `RECEIVED`, sets `received_at`, or clears
`pending_receipt`. The coordinator never executes `SEND_OFFER`, creates or
accepts a Trade Offer, uses Steam Guard, or performs any real Steam/BUFF write.

## Lifecycle and runtime boundary

The caller owns Store initialization and closure. This module does not call
Store lifecycle methods, create SQLite connections, execute SQL, migrate schema,
or repair a database. It is not connected to Pipeline, Purchase Flow, workers,
runtime registration, or Web UI.

Future work may consume the immutable step result, but no future runtime or
platform-write behavior is introduced here.
