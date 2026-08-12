# Auto Offer Confirmation Runtime Contract

TASK-034 connects the already-reviewed exact Steam mobile-confirmation foundation to the active Auto Offer runtime. It does not introduce a second executor, Store, state machine, worker, or credential owner.

## Authority

The active confirmation authority is:

`Host next-purchase gate -> DeliveryCoordinator -> SteamTradeOfferConfirmationAdapter -> SteamTradeOfferConfirmationTransport`

`DeliveryCoordinator` remains the sole active platform-step/write-attempt/state-transition authority. `AutoOfferStore` remains the sole Auto Offer persistence/CAS owner. `Reconciliation` remains the read-evidence planner. Historical `DeliveryExecutor` remains test/compatibility-only.

## Least-privilege construction gate

Historical `allow_writes=True` continues to authorize the existing SEND_OFFER route only.

Runtime confirmation registration additionally requires:

`allow_confirmation_writes=True`

The default is false. Only the explicitly enabled active Host bridge sets both gates. There is no new user-facing configuration switch and no implicit expansion of historical write authority.

## Exact write entry

A confirmation mutation is reachable only for an exact persisted buyer delivery with:

- `delivery_mode == BUYER_SENDS_OFFER`;
- `delivery_status == OFFER_CONFIRMATION_REQUIRED`;
- immutable exact `steam_tradeoffer_id` already bound;
- valid existing buyer send timing and identity contract.

`OFFER_SENT` is still read first. Exact Steam `CREATED_NEEDS_CONFIRMATION` evidence is what may persist `OFFER_CONFIRMATION_REQUIRED`.

`OFFER_CONFIRMATION_ATTEMPTED` and bound confirmation `RESULT_UNKNOWN` never route to a second confirmation mutation. They route only to exact `READ_STEAM_TRADE_OFFER` recovery.

Persisted buyer `AWAITING_OFFER` remains non-sendable and restart cannot recreate first-send authority.

## Durable pre-mutation marker

Before the adapter can execute, Coordinator must successfully CAS:

`OFFER_CONFIRMATION_REQUIRED -> OFFER_CONFIRMATION_ATTEMPTED`

Only the newly persisted attempted revision is allowed to construct the exact `CONFIRM_OFFER` request. A failed/stale CAS means zero confirmation adapter calls.

This is the crash boundary: once `OFFER_CONFIRMATION_ATTEMPTED` exists, runtime must never assume the mutation did not happen.

## Result mapping

### Exact typed success

Only `PlatformResultStatus.SUCCESS` with exact `ConfirmOfferEvidence` for the same Trade Offer and Steam account may persist:

`OFFER_CONFIRMATION_ATTEMPTED -> OFFER_CONFIRMED`

The Host stops that delivery at `OFFER_CONFIRMED` for the current gate call. It does not immediately poll Steam after the mutation. Later host-gate calls continue the normal exact-read progression.

### Explicit ambiguous write outcome

`PlatformResultStatus.RESULT_UNKNOWN` persists:

`OFFER_CONFIRMATION_ATTEMPTED -> RESULT_UNKNOWN`

with `delivery_error == "write_result_unknown"` and the exact bound Trade Offer ID retained.

No confirmation mutation is retried. Later recovery is exact `READ_STEAM_TRADE_OFFER` only.

### Proven preflight / non-ambiguous failure

`TIMEOUT`, `FAILURE`, `MALFORMED`, or `UNSUPPORTED` creates no post-attempt success state and does not retry. The durable `OFFER_CONFIRMATION_ATTEMPTED` row remains. The current host gate fails/stops closed; a later gate may only perform exact read recovery.

## Exact transport and credential ownership

The active Host bridge constructs the existing TASK-029 exact confirmation stack and reuses the same authenticated Steam session/account boundary as the exact read transports.

Credentials come only from the TASK-030 canonical Steam credential owner:

- exact SteamID;
- Steam cookie string;
- canonical `identity_secret`.

The identity secret is passed only into the confirmation transport. It is not copied into app configuration, logs, Store rows, errors, requests returned to callers, or test evidence.

TLS verification must remain enabled.

The transport remains exact-ID and bounded:

- exact full Trade Offer ID lookup;
- at most one exact `allow` mutation for a durable attempt;
- no first/latest/fuzzy/suffix/creator-id fallback;
- no bulk confirmation;
- no `accept_all`;
- no legacy `app.steam_confirm` reuse.

## Trigger and retry policy

The existing synchronous bounded next-purchase host gate remains the only runtime trigger.

TASK-034 adds no:

- worker;
- thread;
- task queue;
- scheduler;
- timer;
- sleep;
- polling loop;
- automatic retry;
- confirmation resend.

A later gate is state-driven recovery, not a retry loop.

## Verification boundary

TASK-034 implementation verification must use fake/injected transports and sessions only. No real Steam, BUFF, or mobile-confirmation request is part of the verification matrix.

The canonical source requires disposable Windows exact-tree verification with pre/post tree identity, focused confirmation tests, historical Auto Offer/host regressions, full pytest, the 1438 CI baseline, `pip check`, `git diff --check`, and static no-retry/no-legacy-confirm/no-schema proof.

`F:\AetherSwap` remains protected and must not be accessed by the verifier.

## Live-write gate

**CLOSED during TASK-034 implementation and verification.**

A real single-order mobile-confirmation canary requires a separate explicit OWNER authorization after TASK-034 is merged, post-merge CI is green, and the post-TASK-034 historical/simplicity review passes.

TASK-034 itself does not authorize a real Steam/BUFF write and does not authorize merging to `main`.
