# Auto Offer Platform Read-Only Adapters

TASK-009 adds a thin, fail-closed boundary between the native Auto Offer
contracts and existing platform readers.  `BuffReadOnlyAdapter` accepts an
injected object exposing `get_steam_trades()`.  `SteamInventoryReadOnlyAdapter`
accepts an injected reader bound to one `account_id` and one
`recipient_steam_id`.

The module reuses `PlatformRequest`, `PlatformResult`, `PlatformAdapter`,
`PlatformCapability`, and `PlatformResultStatus` from TASK-008.  It does not
create credentials, sessions, network clients, persistence, workers, or
runtime registration.

## Safety contract

- Only explicit canonical `buff_order_id` or `bill_order_id` fields may prove
  a BUFF order.  Generic `id`, goods IDs, names, positions, and time ordering
  are never used as purchase identity.
- BUFF direction succeeds only when the unique order record proves a seller
  sends to the exact requested recipient.  `SEND_OFFER` is unsupported and is
  rejected before the injected client is touched.
- BUFF offer state succeeds only for a unique exact order with a valid offer ID
  and a known pending state.
- Steam inventory success means only that a structurally valid snapshot was
  read for the exact bound identity.  It is not receipt proof and does not
  update a purchase, delivery snapshot, Store, or executor.
- Timeout, authentication failure, ordinary client failure, malformed data,
  unsupported capability, and insufficient evidence all remain non-success
  results.
- Production network behavior is supplied by the host through dependency
  injection.  Tests use fakes only; this module performs no live network call,
  retry, sleep, thread work, or platform mutation.

## Scope

The TASK-009 business diff is limited to:

- `app/auto_offer/platform_readonly.py`
- `tests/test_auto_offer_platform_readonly.py`
- `.agent/AUTO_OFFER_PLATFORM_READONLY.md`

Existing BUFF, Steam, Store, Executor, Purchase Flow, Pipeline, and Worker
files remain read-only dependencies.
