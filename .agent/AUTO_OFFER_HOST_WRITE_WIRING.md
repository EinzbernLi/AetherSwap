# Auto Offer host write wiring — TASK-028

## Scope

TASK-028 connects the already-reviewed buyer SEND_OFFER transport to the host lifecycle without creating another executor, worker, scheduler, or state machine.

The active authority remains:

`Host purchase persistence -> AutoOfferStore -> DeliveryCoordinator -> platform adapter`

`DeliveryCoordinator` remains the sole owner of platform-step routing and durable state transitions.

## Enable authority

`auto_offer.enabled` remains default-off. TASK-027 made the persisted validated flag the sole source for a new pipeline run snapshot. TASK-028 does not add another runtime toggle or live config owner.

Disabled runs retain the historical host path and construct no Auto Offer platform dependency.

## First-send authorization

A host purchase is appended durably before Auto Offer registration is called.

`AutoOfferStore.ensure_initial_with_created()` performs one `BEGIN IMMEDIATE` transaction and distinguishes:

- a new initial delivery inserted by the current checkout (`created=True`), from
- an exact pre-existing duplicate (`created=False`).

Only `created=True` produces an in-memory first-send authorization token for the current run. `ensure_initial()` remains the historical compatibility API.

A pre-existing row from a prior process/run can block or be reconciled, but it can never become a delayed first-send authorization in TASK-028.

## Batch boundary

Per-record registration performs Store I/O only. It performs no network request.

The host may persist several records inside one already-paid BUFF batch. Fresh delivery tokens are dispatched only when the BUFF checkout guard is fully resolved:

- before a later purchase via the existing host purchase gate, or
- at integration finalization if the successful checkout reached the run target and there is no later purchase.

If checkout is partial, unknown, or otherwise unresolved, finalization performs no first-send write.

This guarantees all confirmed records for the checkout are locally durable before any Auto Offer platform write.

## Runtime identity boundary

Before a fresh authorization token is consumed or any platform read/write is executed, the integration re-resolves the current host account and proves:

- current account ID equals the run-bound account ID;
- current account SteamID equals the run-bound recipient SteamID;
- current Steam credential SteamID equals that same recipient identity.

A post-payment account/credential switch therefore blocks before platform I/O. The SEND adapter also keeps its execution-time cookie identity provider as a second fail-closed check at the non-idempotent boundary.

## Host BUFF facade ownership

The object owned by the buy pipeline is `app.services.buff_client.BuffClient`, not the raw `BuffBuyer`.

TASK-028 preserves that ownership instead of reaching through private attributes:

- `BuffClient.get_buy_orders_waiting_to_send_offer(...)` delegates the TASK-023 buyer-direction read through the facade's existing `_run(...)` boundary;
- `BuffClient.send_buyer_offer(...)` delegates one exact operation through the same `_run(...)` boundary and constructs the existing TASK-025 `BuffBuyerSendTransport` only around the currently owned underlying buyer for that call;
- the facade therefore retains its authentication lock, client lock, credential-generation refresh, and rotated-cookie persistence semantics around both operations;
- `HostAutoOfferIntegration` never accesses `BuffClient._run`, `BuffClient._buyer`, or `BuffBuyer._make_request`.

`send_buyer_offer(...)` is deliberately narrow. It is not a generic write/request primitive and adds no retry. The existing TASK-025 transport still owns buyer-info crypto, exact order/Steam identity preflight, and the one non-idempotent POST.

## Bounded synchronous dispatch

For each fresh delivery, in registration order:

1. execute exactly one `READ_DELIVERY_DIRECTION` Coordinator step;
2. seller-send direction performs no buyer SEND_OFFER;
3. buyer-send `AWAITING_OFFER` executes exactly one Coordinator SEND_OFFER step;
4. the Coordinator persists `OFFER_ATTEMPTED` before calling the non-idempotent adapter;
5. any unproven send result becomes `RESULT_UNKNOWN`;
6. after `RESULT_UNKNOWN`, execute at most one immediate `READ_OFFER_STATE` recovery step using the TASK-026 exact-evidence path;
7. if recovery does not prove `OFFER_SENT`, stop later first-send dispatch from that checkout.

Fresh authorization tokens are consumed before platform execution begins. No exception or later gate call can cause an automatic resend.

There is no polling loop, timer, scheduler, retry worker, or automatic recovery worker.

## Adapter composition

The enabled host bridge owns one Store, one Steam read session, and one `DeliveryCoordinator(allow_writes=True)` registry containing only:

- `READ_DELIVERY_DIRECTION`
- `READ_OFFER_STATE`
- `READ_STEAM_TRADE_OFFER`
- `READ_STEAM_COMPLETED_TRADE`
- `SEND_OFFER`

The SEND capability is the existing `BuffBuyerSendOfferAdapter` backed by a transport-shaped host wrapper around public `BuffClient.send_buyer_offer(...)`. That public facade method in turn executes the existing `BuffBuyerSendTransport` exactly once inside `BuffClient` ownership.

Steam credentials are checked against the exact host SteamID both at bridge construction and again through the SEND adapter's execution-time cookie provider.

No host code calls the underlying BUFF request method directly.

## Failure semantics

- missing or ambiguous direction evidence: no send;
- send timeout, malformed response, arbitrary transport exception, or unproven BUFF `OK`: `RESULT_UNKNOWN`;
- `RESULT_UNKNOWN` never transitions back to a resendable state;
- unresolved checkout: no first-send dispatch;
- identity mismatch or Store mismatch: fail closed / next purchase blocked;
- one unresolved write stops later first-send writes from the same checkout.

TASK-026 remains the only automatic read-evidence recovery path for `OFFER_ATTEMPTED` / `RESULT_UNKNOWN`.

## Legacy ownership

When Auto Offer is enabled, the existing pipeline continues to suppress `buff.auto_ask_seller_to_send` only in its ephemeral checkout config so the legacy seller-reminder write does not compete with Auto Offer delivery ownership.

When Auto Offer is disabled, historical seller-reminder behavior is unchanged.

`DeliveryExecutor` remains historical/test-only and is not imported or used by the active path.

## Deferred work

TASK-028 does not implement Steam mobile confirmation or offer acceptance/confirmation automation. That remains a later separately frozen task.

The TASK-025 transport still internally depends on raw `BuffBuyer._make_request`; TASK-028 does not generalize or duplicate that primitive. The host-facing layer depends only on the narrow public `BuffClient.send_buyer_offer(...)` method. The duplicated strict Steam-cookie parsing debt also remains intentionally deferred.

## Real-write gate

CLOSED during implementation and verification.

All TASK-028 local and CI tests must use fake/injected platform I/O. No real Steam/BUFF request is authorized by this task. A real single-order canary requires separate OWNER approval after exact-SHA Windows verification, GitHub CI, integration merge, and post-merge review.
