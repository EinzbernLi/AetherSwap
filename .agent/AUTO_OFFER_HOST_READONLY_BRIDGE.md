# TASK-021 Auto Offer Host Readonly Bridge

Status: implementation branch, awaiting Windows verification and Web GPT acceptance.

- Task: TASK-021
- Issue: #49
- Architecture: `native-auto-offer-module-v1`
- Execution base: `integration/auto-buyer-offer @ f1e3880d935d79c4259cf26d62021f4e619cbeb9`
- Branch: `luna/TASK-021-host-readonly-bridge`
- Risk: HIGH
- Default enabled: `False`

## Purpose

TASK-021 adds an explicit host-facing bridge around the reviewed TASK-020
read-only runtime.  The bridge is deliberately not attached to AetherSwap
startup, Pipeline, workers, receive flow, routes, UI, or any scheduler.

The host must resolve and pass dependencies explicitly.  Every platform step
must also be requested explicitly by the caller.

## Default-off behavior

`build_host_readonly_auto_offer_bridge()` uses
`AUTO_OFFER_DEFAULT_ENABLED`, which remains `False`.

When disabled, the factory returns `None` before inspecting Store path, account
identity, Steam credentials, BUFF client, timeout, or constructing a Session.
No file, database, Session, or network operation is created by the disabled
path.

## Identity binding

Enabled construction requires one exact identity chain:

```text
host account SteamID
== steam_credentials["steam_id"]
== steamLoginSecure-bound SteamID validated by TASK-020 transports
```

`account_id` is a non-empty trimmed string.  Steam IDs are canonical positive
decimal strings.  No username, display name, market name, timestamp, BUFF item
name, or fuzzy identity is used.

A mismatch fails closed.  The bridge never reads `app.accounts`, host config,
passwords, Steam Guard secrets, mobile-confirmation secrets, or credential
storage itself.

## BUFF boundary

The bridge accepts an already-owned BUFF client and validates only the existing
read-only `get_steam_trades` boundary required by TASK-020.

It never constructs a BUFF client and never calls checkout, payment, finalize,
seller-reminder, or any other BUFF write method.

## Steam Session boundary

Enabled construction creates one fresh `requests.Session` locally, preserves
TLS verification, and injects that same Session into TASK-020.  TASK-020 in turn
uses it for both reviewed Steam read transports.

Construction performs no Steam request.  The bridge never imports or calls the
legacy `steam.session.create_market_session` helper and never imports or calls
legacy `app.receive_flow.accept_steam_trade_offer`.

## Store ownership

The bridge owns one independent `AutoOfferStore` using the explicitly supplied
path.  It calls `store.initialize()` only after enabled dependency validation
and Session construction.

Construction order is:

1. validate enabled host identities, Steam credentials, BUFF read dependency,
   Store path, and timeout;
2. construct one safe Session;
3. construct and initialize the independent Auto Offer Store;
4. construct TASK-020 runtime with the exact same identities and Session;
5. return the bridge only after all gates pass.

If construction fails after resources exist, the Store and Session are closed
before a sanitized bridge configuration error is returned.  Host `app.db` is
not modified by this bridge.

## Committed-purchase registration

`register_committed_purchase(record)` is only for a host purchase that a future
host integration task has already durably committed.

Required record evidence is:

- exact non-empty trimmed `buff_order_id`;
- `pending_receipt is True` exactly;
- no recipient `assetid` already present.

The Auto Offer identity is deterministic:

```text
purchase_id = "buff:" + buff_order_id
```

The inserted initial delivery is exactly `PENDING_DIRECTION`, has no delivery
mode or Trade Offer ID, keeps `pending_receipt=True`, and binds the bridge's
exact `account_id` and recipient SteamID.

Persistence uses only `AutoOfferStore.ensure_initial()`.  Exact duplicate
registration is idempotent; conflicting identity fails closed.  Registration
performs no platform request and does not mutate the host record or host
transaction database.

## One-shot boundary

The bridge exposes only explicit local/recovery operations:

- `list_recoverable()` delegates to Store recovery listing;
- `step(delivery)` delegates exactly one call to TASK-020 runtime;
- `close()` closes the bridge-owned Store and Session.

There is no automatic next step, loop, retry, polling, sleep, timer, thread,
task queue, worker, startup hook, or background scheduler.

TASK-020's exact read-only capability set remains authoritative:

- `READ_DELIVERY_DIRECTION`
- `READ_OFFER_STATE`
- `READ_STEAM_TRADE_OFFER`
- `READ_STEAM_COMPLETED_TRADE`

No write capability is registered or exposed.

## Result semantics

TASK-021 does not reinterpret historical result semantics:

- `DISABLED` and `COMPLETE` may permit a future host purchase;
- `WAITING`, `RESULT_UNKNOWN`, and `BLOCKED` block the next purchase.

The bridge itself does not enforce the Pipeline gate because actual host
attachment is deferred to TASK-022.  No RESULT_UNKNOWN resend or automatic
recovery path is added.

## Legacy receive path remains isolated

The live host still contains a legacy receive path capable of accepting a Steam
Trade Offer and updating host purchase rows.  TASK-021 neither imports nor
calls it.

TASK-022 must independently freeze and test a fail-closed ownership/isolation
gate before the bridge can be attached to actual Pipeline purchase lifecycle.
TASK-022 must also decide how `buff.auto_ask_seller_to_send` behaves when
readonly Auto Offer is enabled so no extra post-commit BUFF write is triggered
silently.

## Frozen file scope

Final TASK-021 business diff is exactly:

1. `app/auto_offer/host_readonly.py`
2. `tests/test_auto_offer_host_readonly.py`
3. `.agent/AUTO_OFFER_HOST_READONLY_BRIDGE.md`

No historical file may be modified.  If this scope proves insufficient, stop
with `SCOPE_BLOCKED` rather than expanding it.

## Verification gates

Windows verification uses:

`E:\python\python.exe`

with pytest `9.1.1`.

Required:

- target tests passed == collected;
- Store/runtime/Steam transport/coordinator/reconciliation regressions pass;
- full suite has zero failures, errors, collection errors, or skips;
- baseline gate passes with minimum 447;
- no skip/xfail/deselect/ignore;
- no pytest artifact committed;
- `F:\AetherSwap` untouched;
- no real Steam/BUFF request or write.

After merge, Mandatory Historical Review must pass before TASK-022 planning can
begin.
