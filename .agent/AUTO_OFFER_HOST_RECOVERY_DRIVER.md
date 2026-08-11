# AUTO_OFFER_HOST_RECOVERY_DRIVER

TASK-032 freezes the host-side runtime closure for persisted Auto Offer deliveries.

## Authority

The active authority chain remains:

`Host trigger -> DeliveryCoordinator -> Reconciliation -> AutoOfferStore CAS`

`DeliveryExecutor` remains historical/test-only and MUST NOT be wired as runtime authority.

No second Store, state machine, planner, executor, worker, scheduler, timer, polling loop, or retry framework is permitted.

## Trigger

Persisted recovery runs only inside the existing synchronous next-purchase host gate.

One gate call is finite. It may advance through adjacent read-proven states only while each prior Coordinator step persisted a strict revision/state advance. A no-progress read stops immediately. There is no sleep, polling, or immediate repeat merely because platform state might change later.

## First-send boundary

Fresh buyer first-send authority exists only for the in-memory token returned by the exact Store insert performed for the current checkout.

A persisted or restarted:

`BUYER_SENDS_OFFER + AWAITING_OFFER`

MUST NEVER call the write-enabled Coordinator step, because that route is `SEND_OFFER`.

It remains WAITING/blocking. Restart never recreates first-send authority.

`OFFER_ATTEMPTED` and eligible unbound SEND `RESULT_UNKNOWN` recover only through the existing exact read route. No resend is permitted.

## Safe persisted read states

The host recovery driver may call Coordinator only for current states whose route is read-only:

- `PENDING_DIRECTION`;
- seller `AWAITING_OFFER`;
- buyer `OFFER_ATTEMPTED`;
- eligible buyer `RESULT_UNKNOWN`;
- seller `OFFER_RECEIVED`;
- buyer `OFFER_SENT`;
- buyer `OFFER_CONFIRMATION_REQUIRED`;
- buyer `OFFER_CONFIRMATION_ATTEMPTED`;
- `OFFER_CONFIRMED`;
- `AWAITING_INVENTORY`.

Unsupported or malformed shapes fail closed.

## Confirmation boundary

TASK-032 does not runtime-wire `CONFIRM_OFFER`.

It does not import or call the confirmation adapter/transport, does not use `accept_all`, and does not reuse the legacy confirmer.

`CREATED_NEEDS_CONFIRMATION` remains WAITING with no resend or confirmation mutation.

## Exact host-set gate

Before persisted recovery platform reads, host pending purchases and Store recoverables must be reconciled by exact BUFF order identity and exact account/recipient identity.

Pending host rows used by Auto Offer require an exact positive `_db_id` so final receipt persistence cannot use positional mutation.

No fuzzy adoption, first/latest match, name/goods match, or Store repair is allowed.

## Receipt proof ownership

Receipt attribution remains exclusively in the existing completed-trade / reconciliation contract.

Host integration never chooses an asset and never reinterprets Steam completed-trade evidence.

Only an exact, contract-valid persisted AutoOfferStore snapshot with:

- `delivery_status == RECEIVED`;
- `pending_receipt == False`;
- exact non-empty `assetid`;
- matching deterministic `purchase_id` / `buff_order_id` / account / recipient

may authorize the local host Purchase writeback.

## Host persistence closure

The host database owns one narrow exact/idempotent receipt completion primitive.

It must atomically require:

- strict positive primary-key DB id;
- exact BUFF order id on that row;
- pending receipt with no existing asset for first completion, or exact same already-completed asset for idempotent replay;
- no different Purchase already owning the exact asset id.

Only then may it write:

- `assetid = proven assetid`;
- `pending_receipt = False`.

Any mismatch fails without mutation.

Auto Offer code must not access host SQLite directly.

## Interrupted local writeback

AutoOfferStore may already be terminal RECEIVED while the host Purchase is still pending if the process failed between the two local commits.

A later host gate may perform an exact Store lookup by deterministic purchase id and retry only the idempotent local host receipt writeback.

It must not replay any Steam or BUFF platform write.

If local writeback fails again, the gate remains BLOCKED and Store RECEIVED is not reverted.

## Default-off behavior

When Auto Offer is disabled, the host does not construct the Auto Offer bridge and does not require or inspect the receipt writer callback.

Historical Purchase/receive behavior remains unchanged.

## Safety gates

TASK-032 verification must prove:

- no persisted buyer `AWAITING_OFFER` SEND;
- no resend from attempt/unknown states;
- bounded no-progress recovery;
- exact Store/host identity;
- exact idempotent host receipt writeback;
- no confirmation mutation;
- no worker/poller/timer/sleep/retry framework;
- no Auto Offer schema change;
- no real Steam/BUFF request or write.

REAL-WRITE GATE remains CLOSED.
