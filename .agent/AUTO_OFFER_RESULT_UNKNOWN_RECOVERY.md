# Auto Offer buyer write-result recovery

TASK-026 closes the read-only recovery gap left intentionally by TASK-024 and TASK-025. It does not authorize or perform a new buyer-send write.

## Scope and authority

The single authority remains:

`PlatformAdapter -> DeliveryCoordinator -> Reconciliation -> AutoOfferStore CAS`

TASK-026 adds no executor, Store, journal, worker, scheduler, timer, polling loop, retry loop, or runtime write hook.

The only production files changed are `coordinator.py` and `reconciliation.py`.

## Recovery states

Buyer-side records may reach either:

- `OFFER_ATTEMPTED`: the durable attempt was recorded, but the process may have crashed before or during the non-idempotent POST;
- `RESULT_UNKNOWN`: the POST was invoked or its outcome could not be proven safely.

Neither state is resendable.

A later `.step()` from either state is strictly read-only.

## Exact evidence chain

TASK-026 deliberately reuses existing contracts rather than adding a new capability or evidence type.

First read:

`READ_OFFER_STATE`

The existing BUFF read adapter reads `/api/market/steam_trade` and may emit `OfferStateEvidence` only after exact order binding, exact recipient SteamID binding, one unambiguous record, a canonical Trade Offer ID, and a supported pending state.

If that proof is present, Reconciliation may bind the exact ID and recover the buyer record to `OFFER_SENT`.

Second read:

`READ_STEAM_TRADE_OFFER`

The existing Steam Trade Offer reader then validates the exact bound ID. Buyer mode requires an outgoing offer (`is_our_offer=True`) and no outgoing item payload (`items_to_give == ()`) before normal lifecycle progression.

Therefore the recovery proof is:

`exact BUFF order -> exact Trade Offer ID -> exact Steam outgoing Trade Offer`

The write response itself is never upgraded into immediate success.

## Observation timestamp

`OFFER_SENT` keeps its historical invariant that `offer_sent_at` must be present.

A recovered write uses the Coordinator's existing injectable clock only after positive exact BUFF offer evidence is returned. The observed timestamp is passed into the pure planner.

The planner requires:

- a finite non-negative number;
- `observed_at >= offer_attempted_at`.

A missing, invalid, or regressed time cannot be persisted as recovery.

This avoids giving clock ownership to a platform adapter and avoids pretending `offer_attempted_at` is a proven send timestamp.

## RESULT_UNKNOWN cleanup

When exact buyer offer recovery succeeds from `RESULT_UNKNOWN`:

- immutable purchase/order/account/recipient identity is unchanged;
- `offer_attempted_at` is unchanged;
- exact `steam_tradeoffer_id` is bound;
- `offer_sent_at` is set to the validated observation time;
- `delivery_error=write_result_unknown` is cleared;
- status becomes `OFFER_SENT`.

No other error or identity field is rewritten.

## Missing evidence

The following never trigger resend:

- no BUFF row yet;
- no exact Trade Offer ID yet;
- timeout;
- transient read unknown;
- malformed payload;
- ambiguous order evidence;
- identity mismatch;
- clock failure or regression.

Unknown or incomplete evidence leaves the delivery blocked/recoverable according to the existing result contract and continues to block the next purchase.

If the offer has already disappeared from BUFF's frozen pending `steam_trade` surface before exact binding can be proven, TASK-026 does not guess from history, item names, prices, goods IDs, or Steam offer ordering. The delivery remains unresolved for later/manual reconciliation.

## No-resend invariant

The only first-send route remains:

`BUYER_SENDS_OFFER + AWAITING_OFFER -> SEND_OFFER`

`OFFER_ATTEMPTED` and eligible buyer `RESULT_UNKNOWN` route to `READ_OFFER_STATE`, never `SEND_OFFER`.

After exact recovery to `OFFER_SENT`, the next route is `READ_STEAM_TRADE_OFFER`.

There is no transition back to `AWAITING_OFFER` or `OFFER_ATTEMPTED` from `RESULT_UNKNOWN`.

## Existing contracts reused

No change is required to `contracts.py` because it already allows:

- buyer `OFFER_ATTEMPTED -> OFFER_SENT` with first exact Trade Offer binding;
- buyer `RESULT_UNKNOWN -> OFFER_SENT` when later evidence proves a valid forward state;
- no `RESULT_UNKNOWN -> OFFER_ATTEMPTED` transition.

No change is required to `adapters.py` or `platform_readonly.py`; existing `READ_OFFER_STATE`, `OfferStateEvidence`, and `READ_STEAM_TRADE_OFFER` remain authoritative.

## Clean-room reference

Behavioral reference remains `Steamauto/Steamauto @ e803e1ab00cfcede6ef8a7f1b9e784f9bb8da25a`.

The reference sends buyer offers and later returns to the BUFF `steam_trade` read surface rather than consuming an immediate Trade Offer ID from the POST response. AetherSwap independently uses its stricter exact-order and exact-identity read contracts and does not copy the reference's retry behavior.

## Explicit exclusions

TASK-026 contains no:

- POST / PUT / PATCH / DELETE;
- buyer-send invocation;
- Steam offer creation or acceptance;
- Steam confirmation flow;
- credential handling changes;
- runtime/host/Pipeline/worker wiring;
- automatic `.step()`;
- polling loop;
- resend/retry fallback;
- real Steam or BUFF request in tests.

## Live-write gate

The real buyer-send gate remains CLOSED after TASK-026 implementation until:

1. TASK-026 is exact-SHA Windows verified;
2. exact-head CI passes;
3. merge and post-merge CI/review pass;
4. the clean reusable local verification base has been established;
5. host/runtime write wiring is separately frozen;
6. OWNER separately authorizes an exact one-order live canary.

No TASK-026 test or merge itself authorizes a real platform write.
