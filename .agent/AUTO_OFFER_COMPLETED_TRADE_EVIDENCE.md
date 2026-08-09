# TASK-016 — Exact Completed Trade Evidence

Architecture: `native-auto-offer-module-v1`

TASK-016 adds a pure, read-only evidence contract for a completed Steam trade
and an exact recipient inventory confirmation. It is an additive platform
adapter boundary; it does not change delivery state, persistence, planning, or
coordinator routing.

## Evidence chain

The normalized injected reader must bind one exact chain:

`Purchase → exact buff_order_id → immutable steam_tradeoffer_id → exact completed trade → source asset → new asset → exact recipient inventory observation`

`steam_tradeoffer_id` remains the existing `PlatformRequest` identity field and
is required for both `READ_STEAM_TRADE_OFFER` and
`READ_STEAM_COMPLETED_TRADE`. The completed-trade evidence and the request must
match the trade-offer ID and recipient Steam ID exactly.

`steam_trade_id` proves only that a completed trade record was observed. It is
evidence, not permission to mutate a Store or platform state.

## Typed evidence

`CompletedTradeItemEvidence` records source-side and post-trade identities:
`appid`, `contextid`, `assetid`, `amount`, `new_contextid`, and `new_assetid`.
`RecipientInventoryItemEvidence` records an exact identity observed in the
recipient inventory snapshot.

`SteamCompletedTradeEvidence` is immutable and validates strict IDs, finite
non-negative `completed_at`, canonical tuple ordering, duplicate source and
post-trade identities, and the inventory-confirmation subset invariant. Every
inventory confirmation must exactly match an `items_received` post-trade
identity including appid, context, asset ID, and amount. A subset is valid;
unconfirmed received items remain unproven.

Multiple received items are valid evidence. The contract deliberately has no
`purchase_assetid`, `selected_assetid`, `matched_assetid`, or primary-item
field. It never selects an item by position, recency, name, price, or asset ID
ordering.

## Standalone read-only adapter

`SteamCompletedTradeReadOnlyAdapter` consumes only an injected
`SteamCompletedTradeReader` normalized boundary. After exact capability and
account/recipient gates, it performs one reader call with:

`(steam_tradeoffer_id, recipient_steam_id)`

It does not create sessions, perform HTTP, authenticate, retry, poll, sleep,
or run background threads. Reader failures are normalized without exposing
credentials, exception text, or payloads.

## Safety boundaries

- `SUCCESS` does not mean `DeliveryStatus.RECEIVED`.
- Trade source/post-trade asset identities are not by themselves Purchase
  ownership attribution.
- Recipient inventory confirmation proves only the exact observed inventory
  identity and its relation to a received post-trade item.
- Multi-item completed trades remain fail-closed for Purchase attribution.
- No Store, `Store.advance`, SQLite, `DeliverySnapshot`, planner,
  coordinator, runtime, or delivery-state mutation is used.
- No SEND_OFFER, ACCEPT_OFFER, Steam Guard, confirmation, inventory mutation,
  real Steam/BUFF request, or platform write is implemented.

TASK-017 may be considered only after its required Historical Review passes.
