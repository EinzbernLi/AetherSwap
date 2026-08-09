# Auto Offer Read-Only Runtime Contract — TASK-020

Status: implementation contract for TASK-020.

Architecture: `native-auto-offer-module-v1`.

Execution base: `integration/auto-buyer-offer @ 144b22c8bb836c2ae5fc7c6c370289b94ec21e9a`.

## Purpose

TASK-020 introduces one explicit, module-owned, default-disabled read-only runtime factory. It combines the already reviewed BUFF read adapter, TASK-019 exact Steam Trade Offer transport, TASK-018 exact completed-trade transport, and the existing `ReadOnlyDeliveryCoordinator`.

It does not attach Auto Offer to the host application.

## Default-off invariant

`AUTO_OFFER_DEFAULT_ENABLED` remains `False`.

`build_readonly_auto_offer_runtime()` returns `None` immediately when disabled. The disabled path must not inspect or invoke Store, BUFF, Steam cookie, Steam session, identity, timeout, filesystem, or host state.

`enabled` is an exact bool; truthy substitutes fail closed.

## Explicit dependency injection

When enabled, the caller must provide all dependencies explicitly:

- already-owned Store-like object compatible with `ReadOnlyDeliveryCoordinator`;
- already-owned BUFF client exposing `get_steam_trades`;
- exact Auto Offer account ID;
- exact canonical recipient SteamID64;
- already-owned Steam cookie string;
- already-owned/injected Steam HTTP session;
- finite positive timeout.

The factory never reads `config`, `app.config_loader`, `app.accounts`, Pipeline, worker, startup, API, UI, or credential files. It never constructs `BuffClient` and never calls `store.initialize()`.

## Zero-network construction

Enabled construction is local object construction only. It creates:

1. `SteamTradeOfferHttpReader`;
2. `SteamCompletedTradeHttpReader`;
3. `BuffReadOnlyAdapter`;
4. `SteamTradeOfferReadOnlyAdapter`;
5. `SteamCompletedTradeReadOnlyAdapter`;
6. `ReadOnlyDeliveryCoordinator`;
7. `ReadOnlyAutoOfferRuntime`.

No platform request occurs during construction. No login, relogin, refresh, confirmation, retry, sleep, poll, thread, worker, or scheduler is permitted.

## Steam identity cross-binding

Before runtime construction succeeds, caller-supplied `recipient_steam_id` must be canonical positive decimal and exactly equal the strict `steamLoginSecure` bound account exposed by both reviewed Steam transports.

Mismatch fails closed before HTTP I/O with a sanitized runtime configuration error.

The runtime never accesses transport private credential fields and never exposes cookie, access token, session, or raw client internals.

## Capability registry

The registry is exactly:

- `READ_DELIVERY_DIRECTION` -> one `BuffReadOnlyAdapter`;
- `READ_OFFER_STATE` -> the same `BuffReadOnlyAdapter`;
- `READ_STEAM_TRADE_OFFER` -> TASK-019 reader + existing adapter;
- `READ_STEAM_COMPLETED_TRADE` -> TASK-018 reader + existing adapter.

The registry excludes:

- `READ_INVENTORY_STATE`;
- `SEND_OFFER`;
- acceptance/confirmation writes;
- unknown or future capabilities.

Only the immutable `READONLY_RUNTIME_CAPABILITIES` view is public. The mutable adapter registry is not exposed.

## Runtime boundary

`ReadOnlyAutoOfferRuntime.step(delivery)` delegates exactly one call to the existing coordinator.

Production runtime code contains no loop. The coordinator remains the sole owner of:

- one Store read;
- at most one adapter execution;
- planner invocation;
- at most one planner-approved Store CAS advance.

The runtime does not call Store `advance()` directly and does not bypass reconciliation.

## Frozen historical routing

TASK-020 does not modify coordinator or reconciliation. Existing routing remains authoritative:

- `PENDING_DIRECTION` -> BUFF direction read;
- seller `AWAITING_OFFER` -> BUFF offer-state read;
- seller `OFFER_RECEIVED` -> exact Steam Trade Offer read;
- buyer `OFFER_SENT` -> exact Steam Trade Offer read;
- `OFFER_CONFIRMED` -> exact Steam Trade Offer read;
- `AWAITING_INVENTORY` -> exact Steam Completed Trade read.

`RESULT_UNKNOWN` has no resend/write recovery path here.

## Receipt boundary

TASK-019 ACTIVE/ACCEPTED evidence never proves receipt. ACCEPTED advances only to `AWAITING_INVENTORY`.

Only TASK-018 completed-trade evidence plus TASK-017 exact single-item recipient inventory confirmation may produce `RECEIVED`.

Multi-item attribution, outgoing completed-trade items, absent exact inventory confirmation, identity mismatch, direction mismatch, and stale Store state remain fail-closed under the existing planner/coordinator contracts.

## Host isolation

TASK-020 does not modify or attach to:

- `app.auto_offer.__init__`;
- application startup;
- Pipeline;
- pipeline steps;
- receive flow;
- workers;
- timers;
- queues;
- API routes;
- UI;
- purchase-completion hooks;
- config or credential loaders.

Importing `app.auto_offer` must still have no runtime construction or registration side effect.

## Verification

Automated tests use fake/injected BUFF, Store, and HTTP dependencies only. They prove:

- default-disabled dependency tripwires remain untouched;
- enabled construction is zero-I/O;
- exact four-capability surface;
- Steam cookie/recipient pre-network cross-binding;
- TLS/session rejection remains fail closed;
- runtime representation contains no secret material;
- full seller read-only chain reaches `RECEIVED` only through exact completed-trade proof;
- inventory absence stays waiting;
- multi-item and outgoing completed trades block;
- wrong Trade Offer direction blocks;
- `RESULT_UNKNOWN` invokes no platform reader;
- stale Store CAS is not retried;
- no host-wiring/background imports.

Historical TASK-017/018/019 tests remain mandatory regression gates.

## Explicit non-goals

TASK-020 contains no real Steam/BUFF verification, no platform write, no Trade Offer creation/acceptance, no Steam Guard, no confirmation, no Store initialization, and no host integration.

Host integration is a later task after merge, post-merge CI, and Mandatory Historical Review PASS.
