# TASK-005 Native Auto Offer Module Contract

Status: contract-only Draft PR, awaiting Web GPT review.

- Task: TASK-005
- Issue: #14
- Architecture: `native-auto-offer-module-v1`
- Execution mode: `STANDALONE_TOP_LEVEL_RECOVERY`
- Exact base SHA: `831a15af455d826a4b006812bad7891062bc46a3`
- Module ID: `action.auto_offer_delivery`
- Stage: `buy.purchase_committed`
- Default enabled: `False`

## Contract values

`AutoOfferResult` is exactly:

`disabled`, `complete`, `waiting`, `result_unknown`, `blocked`.

Only `disabled` and `complete` permit the next purchase. `waiting`,
`result_unknown`, and `blocked` block it. Unknown inputs fail closed.

`DeliveryMode` is exactly:

- `seller_sends_offer`
- `buyer_sends_offer`

`DeliveryStatus` is exactly:

- `pending_direction`
- `awaiting_offer`
- `offer_attempted`
- `offer_sent`
- `offer_received`
- `offer_confirmed`
- `awaiting_inventory`
- `received`
- `result_unknown`
- `blocked`
- `cancelled`
- `refunded`

## Delivery paths and invariants

Buyer path:

`pending_direction → awaiting_offer → offer_attempted → offer_sent → offer_confirmed → awaiting_inventory → received`

Seller path:

`pending_direction → awaiting_offer → offer_received → offer_confirmed → awaiting_inventory → received`

The direction is unknown at `pending_direction` and must be determined before
`awaiting_offer`. Buyer mode cannot enter `offer_received`; seller mode cannot
enter `offer_attempted` or `offer_sent`.

`result_unknown` means an ambiguous non-idempotent write outcome. It requires
`delivery_error == "write_result_unknown"`, `pending_receipt is True`, and no
`received_at`. It must never transition to `offer_attempted` and has no retry,
force, override, or resend escape hatch. Recovery requires later evidence;
automatic re-sending is prohibited.

Every non-`received` delivery state has `pending_receipt is True`. `received`
requires `pending_receipt is False`, an exact `steam_tradeoffer_id`, an exact
`assetid`, and `received_at`. HTTP success, offer existence, item name, seller
asset ID, or timestamp proximity alone is not receipt proof.

IDs are non-empty strings without leading or trailing whitespace. Timestamps
are finite, non-negative, non-bool numbers and follow
`offer_attempted_at <= offer_sent_at <= received_at` whenever the fields are
present. Delivery errors are restricted to the explicit allowlist.

## Module boundary

TASK-005 freezes a pure, importable contract only. It does not register the
module, add a runtime hook, modify the purchase loop, modify `Purchase`, or
perform any BUFF or Steam operation. User-provided Python, scripts,
entrypoints, shell commands, and arbitrary modules are not executable through
this contract. Monkey patching is prohibited.

The module may use an independent future SQLite store and dedicated adapters
when later tasks authorize them. TASK-006 owns persistence, crash recovery,
and the independent state store. TASK-007 owns native module registration and
the runtime hook. Neither task is started by TASK-005.

No real BUFF/Steam writes were performed.
