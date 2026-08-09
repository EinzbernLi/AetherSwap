# TASK-017 — Completed Trade Receipt Reconciliation

Architecture: `native-auto-offer-module-v1`

Post-TASK-016 Historical Review: **PASS**.

TASK-017 is the first state-planning task allowed to convert merged TASK-016 completed-trade evidence into `DeliveryStatus.RECEIVED`. The platform side remains strictly read-only; the only write is the existing planner-approved `AutoOfferStore.advance()` CAS inside `ReadOnlyDeliveryCoordinator`.

## Exact immutable identity

Both `READ_STEAM_TRADE_OFFER` and `READ_STEAM_COMPLETED_TRADE` are bound to the exact persisted `steam_tradeoffer_id`. Planner identity checking requires the request Purchase, BUFF order, account, recipient Steam ID, revision, and tradeoffer ID to match the exact `StoredDelivery` before any status or evidence interpretation.

TASK-015 immutability remains authoritative through `RECEIVED`; the bound tradeoffer ID is never cleared, normalized, coerced, or rebound.

## AWAITING_INVENTORY routing

Coordinator routing changes from generic `READ_INVENTORY_STATE` to `READ_STEAM_COMPLETED_TRADE` for `AWAITING_INVENTORY`.

The historical planner interpretation of direct `READ_INVENTORY_STATE` evidence remains backward-compatible: a readable inventory snapshot alone still returns `WAITING / purchase_asset_not_proven` and never creates a receipt target.

One coordinator step still permits at most one persisted-current read, one adapter call, one planner call, and one optional Store CAS. There is no same-step fallback to generic inventory reading.

## Receipt proof gate

A completed trade may prove one Purchase receipt only when all of these are true:

1. request/result identity is exact and already bound to the persisted tradeoffer ID;
2. `items_given == ()` — this second zero-outgoing-item gate is checked again at receipt time;
3. `items_received` contains exactly one item;
4. the sole item’s exact `(appid, new_contextid, new_assetid, amount)` appears in `inventory_confirmed_items` as the exact recipient-side `(appid, contextid, assetid, amount)` identity;
5. the resulting `DeliverySnapshot` passes the existing snapshot and transition contracts.

Any outgoing item is `BLOCKED / completed_trade_outgoing_items_present`.

Multiple received items are `BLOCKED / purchase_asset_attribution_ambiguous`. TASK-017 never selects the first/latest/newest item, compares names or prices, uses list order, inventory difference, time proximity, or any other heuristic.

A single item that is not yet confirmed in recipient inventory remains `WAITING / recipient_inventory_not_confirmed` with no persistence.

## Final Purchase asset identity

The source completed-trade `assetid` is not the final Purchase asset ID. On the one positive path, final `DeliverySnapshot.assetid` is exactly the confirmed sole received item’s `new_assetid`.

The transition is exactly:

`AWAITING_INVENTORY -> RECEIVED`

and sets only the receipt fields required by the existing contract:

- `delivery_status = RECEIVED`
- `assetid = sole_received_item.new_assetid`
- `received_at = SteamCompletedTradeEvidence.completed_at`
- `pending_receipt = False`

All immutable identity and previous timing fields are preserved.

`received_at = completed_at` records the completed-trade transfer timestamp supplied by typed evidence. Recipient inventory confirmation is independent corroborating ownership proof; no fuzzy time matching is performed. If existing timestamp ordering or any other snapshot/transition invariant would fail, planning is `BLOCKED / completed_trade_receipt_contract_mismatch` and no Store write occurs.

`steam_trade_id` remains evidence-only and is not persisted in TASK-017.

## Same-step COMPLETE

A proven receipt proposal returns:

- `AutoOfferResult.COMPLETE`
- `retryable = False`
- exact `RECEIVED` target
- `detail = purchase_asset_received`

The coordinator persists that target with exactly one CAS and returns `persisted=True` in the same step. No automatic second step is required.

## Historical behavior preserved

TASK-014 Trade Offer routes and ACTIVE/ACCEPTED semantics remain unchanged. `ACCEPTED` still advances only to `AWAITING_INVENTORY`; it never directly means `RECEIVED`.

`RESULT_UNKNOWN` / no-resend behavior remains unchanged. TASK-017 does not create `OFFER_ATTEMPTED`, call `SEND_OFFER`, retry a write, or introduce a new write-result recovery path.

Store schema v1, optimistic revision CAS, stale-write conflict behavior, and bound tradeoffer immutability are unchanged.

## Explicit safety boundary

TASK-017 does not implement or invoke:

- real Steam history/receipt HTTP;
- BUFF transport;
- SEND_OFFER;
- ACCEPT_OFFER;
- Steam Guard or confirmation;
- inventory mutation;
- Pipeline/workers/runtime registration;
- host Purchase database mutation.

A future independent task must establish the real safe read-only host transport capable of producing the frozen TASK-016 normalized evidence before runtime can depend on this reconciliation path. All non-idempotent write-side tasks remain separately gated by exact identity, duplicate prevention, `result_unknown`, fail-closed behavior, single-order canary, and explicit OWNER authorization.
