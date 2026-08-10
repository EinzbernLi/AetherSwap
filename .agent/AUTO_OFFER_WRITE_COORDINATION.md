# Auto Offer write coordination

TASK-024 freezes the first buyer-side `SEND_OFFER` orchestration boundary without connecting any real BUFF or Steam write transport.

## Authority

The single runtime authority is:

`PlatformAdapter -> DeliveryCoordinator -> AutoOfferStore CAS`

Read evidence continues to use the existing reconciliation planner. Buyer `SEND_OFFER` uses the same Coordinator and Store; there is no second executor, planner, state machine, journal, Store, worker, or scheduler.

TASK-007 `DeliveryExecutor` remains a historical side-effect-free compatibility/test abstraction. It is not runtime write authority and is not wired into TASK-024.

## Default safety

`DeliveryCoordinator` is read-only unless construction explicitly sets `allow_writes=True`. `ReadOnlyDeliveryCoordinator` remains a compatibility alias to the same implementation, so existing readonly runtime and host wiring stay fail-closed without modification.

Only `BUYER_SENDS_OFFER + AWAITING_OFFER` can enter the first-send path.

## Durable ordering

The only allowed first-send sequence is:

1. Re-read and validate the exact persisted delivery and revision.
2. Persist `AWAITING_OFFER -> OFFER_ATTEMPTED` by Store CAS with `offer_attempted_at`.
3. Only after that commit succeeds, build a `SEND_OFFER` request from the new persisted revision.
4. Invoke the configured SEND adapter exactly once.
5. Only exact `SUCCESS + SendOfferEvidence(steam_tradeoffer_id)` may persist `OFFER_SENT` with the same exact offer ID and `offer_sent_at`.
6. Every other invoked outcome is treated as potentially side-effecting and persisted as `RESULT_UNKNOWN / write_result_unknown` when Store persistence is available.

If the attempt CAS fails, the adapter is never called.

## Crash and uncertainty

Once `OFFER_ATTEMPTED` is durable, automatic resend is forbidden.

A crash before the adapter call, an adapter timeout/exception, malformed or non-proven result, or failure while persisting either `OFFER_SENT` or `RESULT_UNKNOWN` leaves a durable state that must not route back to `SEND_OFFER`.

`OFFER_ATTEMPTED` and `RESULT_UNKNOWN` require later read-only reconciliation. TASK-024 does not implement that recovery.

## Success evidence

`SendOfferEvidence` contains only an exact non-empty trimmed `steam_tradeoffer_id`. Bare SEND success or another evidence type is not success.

The SEND request has no pre-existing Trade Offer ID because the offer does not exist before the call.

## Clean-room provenance

Behavioral reference only: `Steamauto/Steamauto` at `e803e1ab00cfcede6ef8a7f1b9e784f9bb8da25a`.

The reference confirms a buyer write endpoint at `POST /api/market/manual_plus/buyer_send_offer` carrying encrypted Steam-cookie material, bill order IDs, and a SteamID. Its generic POST retry behavior is intentionally not copied: this operation is treated as non-idempotent.

No Steamauto source is copied, vendored, or used as a runtime dependency.

## Explicit exclusions

TASK-024 contains no executable BUFF buyer-send POST, Steam offer creation, offer acceptance, Steam confirmation, Steam Guard flow, BUFF encryption, cookie upload, retry loop, polling loop, worker, scheduler, Pipeline hook, or live credential/network test.

The existing readonly runtime and host bridge remain unchanged and do not automatically call the write path.

## Future boundary

A later TASK may connect a real BUFF buyer-send transport only after separately freezing the exact endpoint/request/response contract, clean-room encryption, credential minimization, no-retry transport policy, exact single-order canary, post-attempt reconciliation, and the OWNER gate for any real platform write.
