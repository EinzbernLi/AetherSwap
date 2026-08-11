# Auto Offer Confirmation Recovery Contract

TASK-031 establishes the read-side and durable-state prerequisites for exact Steam mobile-confirmation recovery. It does **not** runtime-wire or execute `CONFIRM_OFFER`.

## Exact Steam evidence

- `SteamTradeOfferHttpReader` remains GET-only and exact-ID bound.
- Only integer Steam `trade_offer_state == 9` is normalized as `created_needs_confirmation`.
- Historical state `2` remains `active`; state `3` remains `accepted`.
- Every other or malformed state remains unproven.
- Exact tradeoffer ID, authenticated account, counterparty, direction, and item-side evidence remain mandatory.
- `CreatedNeedsConfirmation` is read evidence only. It never authorizes a mutation by itself.

## Single durable delivery state machine

TASK-031 adds two buyer-only states to the existing delivery state machine:

- `OFFER_CONFIRMATION_REQUIRED`
- `OFFER_CONFIRMATION_ATTEMPTED`

No second confirmation state machine, worker, table, or executor exists.

The buyer flow can now express:

`OFFER_SENT -> OFFER_CONFIRMATION_REQUIRED -> OFFER_CONFIRMATION_ATTEMPTED -> OFFER_CONFIRMED`

Two evidence-driven shortcuts are intentional:

- `OFFER_SENT -> OFFER_CONFIRMED` remains valid when exact Steam evidence is already `ACTIVE` or `ACCEPTED`; no confirmation write is needed.
- `OFFER_CONFIRMATION_REQUIRED -> OFFER_CONFIRMED` is valid when an external/manual phone confirmation is proven by exact `ACTIVE` or `ACCEPTED` evidence.

Seller delivery semantics are unchanged.

## Crash-safe future confirmation write contract

`OFFER_CONFIRMATION_ATTEMPTED` is the durable pre-mutation marker reserved for a future Coordinator-owned confirmation write.

TASK-031 does not create the write route. The Store/contract layer only proves that the marker can be persisted before any future non-idempotent confirmation request.

A future unproven confirmation write outcome will reuse `RESULT_UNKNOWN` rather than introducing another unknown state. The two write-unknown shapes are distinguished without a new database column:

- SEND unknown: buyer path, no bound `steam_tradeoffer_id`.
- confirmation unknown: buyer path, exact bound `steam_tradeoffer_id` plus existing send timing.

At the snapshot/Store CAS boundary, a **new** transition into `RESULT_UNKNOWN` is permitted only from an explicit durable write-attempt state:

- `OFFER_ATTEMPTED`
- `OFFER_CONFIRMATION_ATTEMPTED`

Historical `RESULT_UNKNOWN` snapshots remain readable so an upgrade cannot turn already-persisted recovery data into database corruption. This compatibility does not permit new arbitrary writes into `RESULT_UNKNOWN`.

## Read-only recovery

The confirmation states and a buyer confirmation `RESULT_UNKNOWN` route only to exact `READ_STEAM_TRADE_OFFER` evidence:

- `CREATED_NEEDS_CONFIRMATION` -> WAITING, no state advance, no resend.
- `ACTIVE` -> `OFFER_CONFIRMED`.
- `ACCEPTED` -> `OFFER_CONFIRMED`.
- timeout, malformed payload, unknown state, identity mismatch, direction mismatch, or unsafe item-side evidence -> fail closed.

A SEND `RESULT_UNKNOWN` with no bound tradeoffer ID keeps the historical BUFF `READ_OFFER_STATE` recovery route. Confirmation unknown cannot fall back to SEND recovery.

Both new statuses are recoverable Store states and therefore remain visible to recovery scans instead of being abandoned.

## Explicit exclusions

TASK-031 does not:

- register `PlatformCapability.CONFIRM_OFFER` in `DeliveryCoordinator`;
- expand `allow_writes` beyond the existing `SEND_OFFER` authority;
- import or call `platform_confirmation.py` or `steam_confirmation_transport.py` from Coordinator/runtime;
- call legacy `app.steam_confirm`;
- add polling, retry, resend, timer, scheduler, background worker, or `accept_all` behavior;
- change SQLite schema version or columns;
- modify host production source;
- perform real Steam or BUFF I/O.

## Verification contract

Canonical source must be verified from a disposable Windows workspace acquired from the exact public GitHub commit archive. Before and after tests, `git write-tree` must equal the expected canonical tree. `F:\AetherSwap` remains protected and untouched.

Focused verification must prove exact state-9 evidence, buyer-only confirmation states, durable Store CAS, RESULT_UNKNOWN separation, exact read recovery, recoverable scanning, next-purchase blocking semantics, and continued Coordinator rejection of `CONFIRM_OFFER`, followed by historical Auto Offer/host/full-suite regression coverage.

## Live-write gate

**CLOSED.** TASK-031 authorizes no real Steam/BUFF request and no mobile-confirmation mutation.
