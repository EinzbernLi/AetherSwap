# TASK-023 — BUFF buyer wait-send direction evidence

## Scope

TASK-023 extends the existing read-only `READ_DELIVERY_DIRECTION` path. It
does not send a Steam Trade Offer, call a BUFF write endpoint, accept or
confirm an offer, start an automatic step, or add runtime scheduling.

The existing seller direction remains authoritative first. The buyer reader
is attempted only after the seller result is exactly
`RESULT_UNKNOWN / order_not_proven`.

## BUFF read contract

The existing `BuffBuyer` session performs one read-only `GET` to:

`https://buff.163.com/api/market/buy_order/wait_send_offers`

with `game=csgo` and `appid=730`. The reader returns only `data.items` when
the response is `code == "OK"`, `data` is a mapping, and `items` is a list.
Malformed or non-OK responses fail closed. Existing authentication and
request-policy blocking exceptions remain propagated; no new session,
retry, sleep, or credential mutation is introduced.

## Exact buyer proof

Buyer direction succeeds only when one and only one endpoint row has:

1. exact `id` equal to the persisted `PlatformRequest.buff_order_id` after
   strict scalar normalization;
2. exact `buyer_steamid` equal to
   `PlatformRequest.recipient_steam_id`; and
3. exact `state_text == "等待你发起报价"`.

The endpoint's `buyer_steamid` field is not aliased. Generic IDs, goods
names, timestamps, prices, list position, and duplicate rows cannot prove
the order. Missing or malformed contract data remains fail closed.

Successful proof returns the existing `DeliveryDirectionEvidence` with
`direction == "buyer_sends_offer"`; it binds no tradeoffer ID.

## Planner boundary

At `PENDING_DIRECTION`, seller evidence continues to propose
`SELLER_SENDS_OFFER / AWAITING_OFFER`. Buyer evidence may propose only
`BUYER_SENDS_OFFER / AWAITING_OFFER`. The existing coordinator boundary then
continues to block the next buyer-mode step with
`write_capability_required`.

## Clean-room reference

Behavioral reference only: `Steamauto/Steamauto` master commit
`e803e1ab00cfcede6ef8a7f1b9e784f9bb8da25a`, as recorded by Issue #57.
No reference source is copied, vendored, or imported at runtime.
