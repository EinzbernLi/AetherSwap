# Auto Offer Trade Offer Binding Hardening

## Historical review finding

The post-TASK-014 historical review found that four-party delivery identity
(`purchase_id`, `buff_order_id`, `account_id`, `recipient_steam_id`) was not
sufficient once a Steam Trade Offer ID had been established.  The TASK-014
planner already preserved the bound ID, but the generic transition and Store
boundaries could not independently reject a later rebind or clear.

TASK-015 hardens that generic boundary without changing the state graph,
platform adapters, planner, coordinator, runtime, or SQLite schema.

## Pre-binding states

`PENDING_DIRECTION`, `AWAITING_OFFER`, and `OFFER_ATTEMPTED` require
`steam_tradeoffer_id=None`.  A normal first binding is permitted only on:

- seller `AWAITING_OFFER -> OFFER_RECEIVED`;
- buyer `OFFER_ATTEMPTED -> OFFER_SENT`.

`RESULT_UNKNOWN` recovery remains compatible: when the current snapshot has no
Trade Offer ID, recovery may first bind an ID only when moving to a later
mode-valid state that already requires a Trade Offer ID.  Arbitrary first
binding to `RESULT_UNKNOWN`, `BLOCKED`, `CANCELLED`, or `REFUNDED` is not
allowed.

## Once-bound immutability

After `steam_tradeoffer_id` becomes non-`None`, every later snapshot transition
must preserve that exact string.  It cannot be normalized, coerced, replaced,
or cleared, including transitions to `RESULT_UNKNOWN`, `BLOCKED`, `CANCELLED`,
`REFUNDED`, or `RECEIVED`.

This is a binding invariant, not an evidence rule.  Existing and future callers
remain responsible for proving the first ID.

## Store defense in depth

`AutoOfferStore.advance()` independently checks an already-bound Trade Offer ID
before beginning the SQLite write path.  A different or cleared ID raises a
contract error without mutating the row or incrementing the revision.  Schema
version 1, WAL/FULL synchronous settings, revision CAS, identity uniqueness,
and recoverable-state behavior remain unchanged.

## Compatibility boundaries

TASK-014 read-only transitions continue to preserve the same exact ID across
`OFFER_RECEIVED/OFFER_SENT -> OFFER_CONFIRMED -> AWAITING_INVENTORY`.
`RESULT_UNKNOWN` no-resend semantics are unchanged.  No changes are made to
`reconciliation.py`, `coordinator.py`, adapters, platform evidence, readers,
or runtime integration.

Future completed-trade/receipt evidence must consume the same immutable bound
Trade Offer ID.  Future `SEND_OFFER`, `ACCEPT_OFFER`, Steam confirmation, and
other write paths may establish an ID only through their explicitly authorized
first-binding transition and may never rebind an existing ID.

No real Steam or BUFF request, platform write, receipt attribution, schema
migration, retry, polling, sleep, or background thread is introduced here.
