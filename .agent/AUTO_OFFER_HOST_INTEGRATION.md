# TASK-022 Auto Offer host integration

TASK-022 attaches the existing TASK-021 read-only Auto Offer bridge to the
host purchase lifecycle with the smallest possible surface. The feature is
default-off and this document describes the host ownership boundary only.

## Scope

- Resolve the current account with `get_current_id()` followed by the exact
  `get_account(current_id)` lookup. A legacy `get_current_account()` fallback
  is not permitted.
- Reuse the pipeline's already-created BUFF client and create one explicit
  `HostReadOnlyAutoOfferBridge` when `auto_offer.enabled` is exactly `True`.
- Keep bridge construction, registration, and reads free of platform I/O.
- Commit the host purchase record first, then register the exact pending
  purchase with the Auto Offer Store. Registration failure is fail-closed.
- Gate every next purchase by comparing the host's exact pending order set to
  the Store's recoverable exact order set. COMPLETE permits; WAITING,
  RESULT_UNKNOWN, and BLOCKED stop the next purchase.
- When enabled, skip the complete legacy receive transaction for that worker
  round. When disabled, preserve the historical receive path.
- When enabled, pass an ephemeral config copy to checkout with seller
  reminders disabled. Never mutate saved configuration.

## Explicit non-goals

This task does not send or accept offers, confirm Steam trades, call an
automatic platform step, add a worker/thread/scheduler/timer/polling loop, or
change the legacy receive implementation. It does not add write-side
platform capabilities.

## Lifecycle

The host owns the existing pipeline and worker lifecycles. The integration is
constructed only for an enabled run, used as the registration and gate seam,
and closed in a `finally` block. Disabled configuration returns before any
Auto Offer account, credential, Store, Session, gate, or callback side effect.

