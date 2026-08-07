# Auto Offer Delivery Executor Contract

TASK-007 introduces a narrow, native execution abstraction between a persisted
`StoredDelivery` and an immutable `DeliveryResult`.

## Boundaries

`app.auto_offer.executor` is deliberately side-effect free. It does not write
the Auto Offer Store, mutate `DeliverySnapshot`, access the network, or import
BUFF, Steam, inventory, pipeline, worker, strategy, or Purchase-flow modules.
The existing contracts and store remain the only authorities for validation and
persistence.

## Public API

* `DeliveryExecutor` is the abstract executor interface.
* `DeliveryResult` contains the exact input `StoredDelivery`, an
  `AutoOfferResult`, and an explicit `retryable` flag.
* `MockDeliveryExecutor` is deterministic and local-only. It derives results
  from the stored contract state; it has no adapter or platform hooks.
* `retry_is_allowed(previous, current)` permits a retry only for a previous
  `waiting` result after the same Purchase has a strictly higher persisted
  revision.

## Fail-closed behavior

Inputs must be an exact `StoredDelivery`, use a positive integer revision, and
pass the frozen delivery snapshot validator. Invalid or unknown values raise
`DeliveryExecutorError`; they do not receive a permissive fallback.

`result_unknown` returns `AutoOfferResult.RESULT_UNKNOWN`, remains blocking,
and is never retryable. `blocked` also remains blocking and non-retryable.
Only `received`, `cancelled`, and `refunded` produce `complete`; all other
nonterminal states produce `waiting`.

## Idempotence and retry

The mock caches outcomes by `(purchase_id, revision)`. Repeating an exact
execution returns the same result and performs no state mutation. A retry is
not an automatic re-execution: it requires an externally persisted, higher
revision. TASK-007 does not create such revisions and does not perform any
real delivery action.

## Deferred work

No module registration, `buy.purchase_committed` integration, worker behavior,
network adapter, Steam action, BUFF action, Trade Offer, inventory access, or
real retry exists in this task. Those changes require a later authorized task.
