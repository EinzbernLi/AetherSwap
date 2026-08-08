# Auto Offer Platform Adapter Interface

TASK-008 defines a pure boundary between the existing Delivery Executor and
future platform-specific implementations.  It adds no runtime registration,
purchase-flow integration, persistence, network access, or real Steam/BUFF
operation.

## Boundary

The adapters module contains only immutable values and a PlatformAdapter
protocol.  Future adapters receive one immutable PlatformRequest and return
one immutable PlatformResult.  The module imports neither the Store nor the
Delivery Snapshot, so it cannot mutate their state.

Each request preserves the exact persisted identity:

* purchase_id
* buff_order_id
* account_id
* recipient_steam_id
* revision

IDs must be exact non-whitespace strings.  Revisions are exact positive
integers; booleans are rejected.  A finite, positive timeout_seconds is only
an abstract caller budget: TASK-008 never waits, sleeps, opens a socket, or
performs a request.

## Capabilities and results

PlatformCapability is an explicit enum, not a free-form string.  It declares
possible future read and send capabilities without authorizing any real
platform action.  The only successful result is
PlatformResultStatus.SUCCESS.  Timeout, unsupported capability, unknown
outcome, adapter failure, and malformed output all remain non-success and
therefore fail closed.

## Deterministic fake

FakePlatformAdapter is local-only and deterministic.  It returns configured
outcomes keyed by the complete immutable request, with a fail-closed unknown
result as the default.  Its test seam can represent timeout, unsupported,
internal-error, malformed, and identity-mismatch cases without real network
access or waiting.

## Deferred work

No Steam API, BUFF API, HTTP, WebSocket, Trade Offer, Steam Guard, inventory
access, authentication, Store mutation, schema change, Purchase Flow, worker,
strategy, retry, or action registration exists in this task.  Integrating a
future real adapter requires a separately authorized task.
