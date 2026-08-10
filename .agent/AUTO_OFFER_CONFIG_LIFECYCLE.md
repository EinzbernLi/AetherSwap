# Auto Offer configuration lifecycle

TASK-027 hardens the control-plane lifecycle for `auto_offer.enabled` before any production write adapter is mounted into the host runtime.

## Single source of truth

The persisted validated application config is authoritative for `auto_offer.enabled` when a new buy-pipeline run starts.

The `/api/pipeline/start` request body remains the source for ordinary per-run pipeline settings, but it cannot enable or disable Auto Offer independently of the persisted flag.

The default remains:

`auto_offer.enabled = false`

## Run snapshot

`start_pipeline()` already owns the pipeline lifecycle boundary. While holding the existing pipeline lifecycle lock it now:

1. reads the persisted validated Auto Offer flag;
2. deep-copies the caller-supplied run config;
3. canonicalizes only `auto_offer.enabled` in that detached copy when necessary;
4. gives the detached copy to the pipeline thread.

A caller cannot mutate the running Auto Offer flag after the start call returns.

When the persisted flag is false and the caller omitted the entire `auto_offer` section, the raw run config is left structurally unchanged. `_run_pipeline()` already applies the schema default `false`, preserving the historical call shape while retaining the same security meaning.

## Toggle serialization

An explicit `/api/config` patch that contains `auto_offer.enabled` uses the same existing pipeline lifecycle lock as pipeline start.

No second lock, config owner, state machine, worker, or scheduler is introduced.

Under that lock the current persisted flag and requested validated flag are compared:

- same value: save is allowed, including a full config document while the pipeline is running;
- actual `false -> true` or `true -> false` transition: allowed only while the buy pipeline is stopped and shutdown is not pending;
- actual transition while the buy pipeline is alive: blocked before persistence;
- actual transition while application shutdown/reset is pending: blocked before persistence.

Because start and transition use the same lifecycle lock, their ordering is deterministic:

- if the transition commits first, the later run snapshots the new persisted value;
- if the run starts first, its snapshot is fixed and the later transition is blocked while that run is alive.

## Unrelated configuration

A `/api/config` patch that does not explicitly contain `auto_offer.enabled` keeps the historical direct validated-update path.

TASK-027 does not freeze unrelated settings merely because the buy pipeline is running.

Full import and data reset retain the existing `exclusive_pipeline_maintenance` authority. TASK-027 does not create a competing maintenance framework.

## Explicit exclusions

TASK-027 does not:

- mount `BuffBuyerSendOfferAdapter` into a host/runtime path;
- enable `allow_writes=True` in the host runtime;
- execute `SEND_OFFER`;
- add POST/PUT/PATCH/DELETE platform transport;
- create a new executor, Store, worker, scheduler, timer, polling loop, retry or resend path;
- change Steam confirmation behavior;
- add UI controls;
- authorize a real Steam or BUFF request.

## Verification policy

Windows verification uses the OWNER-approved disposable exact-source workflow:

`exact GitHub candidate archive -> exact Git tree proof -> tests -> evidence comment -> delete disposable workspace`

No persistent verifier clone is required. `F:\AetherSwap` remains protected and untouched.

## Live-write gate

The live-write gate remains CLOSED after TASK-027.

Host/runtime write wiring is a separately frozen future task. A real one-order canary remains separately OWNER-authorized even after write runtime wiring and fault-injection verification are complete.
