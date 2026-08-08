# Auto Offer Exact Steam Trade Offer Evidence

## Scope

This module is part of `native-auto-offer-module-v1` and provides exact,
read-only Steam Trade Offer evidence. It does not perform a Steam request by
itself: a host supplies a normalized reader through dependency injection.

`READ_OFFER_STATE` remains BUFF order-side evidence. The new
`READ_STEAM_TRADE_OFFER` capability is separate and requires an exact
`steam_tradeoffer_id` in `PlatformRequest`.

## Clean-room behavioral reference

Behavioral reference only:

- Repository: `Steamauto/Steamauto`
- Reference commit: `e803e1ab00cfcede6ef8a7f1b9e784f9bb8da25a`
- Mode: clean-room behavior only

The reference informs endpoint semantics, `IEconService/GetTradeOffer`,
`get_trade_offer(trade_offer_id)`, Trade Offer lifecycle values, item-side
fields, direction, history, receipt, and confirmation behavior. No source is
copied, no Steamauto or steampy dependency is vendored, and no runtime
dependency is introduced.

## Exact request and reader boundary

`READ_STEAM_TRADE_OFFER` requires a strict, non-empty, trimmed
`steam_tradeoffer_id`. Existing capabilities must carry `None` for this field.
The injected `SteamTradeOfferReader` receives exactly one exact ID after
account and recipient binding pass. It does not scan offers, use latest or
nearest matching, retry, poll, sleep, create a session, authenticate, or make
network calls in this task.

`SteamTradeOfferReadOnlyAdapter` binds `account_id` and
`recipient_steam_id`. A request mismatch fails closed before the reader is
called. Reader output is a normalized mapping, never a trusted
`PlatformResult`.

`PlatformResult.__post_init__` defensively revalidates the nested
`PlatformRequest`. This blocks forged requests created with object allocation
or attribute mutation from bypassing identity, revision, timeout, capability,
or Steam Trade Offer ID rules. `FakePlatformAdapter` applies the same boundary.

## Typed evidence

`TradeOfferItemEvidence` contains only strict `appid`, `contextid`, `assetid`,
and positive `amount`. `SteamTradeOfferEvidence` contains exact offer,
account, counterparty, direction, positively known lifecycle, and canonical
item tuples. Duplicate item identity on one side is rejected; both sides may
not be empty; one side may be empty.

Only `ACTIVE` and `ACCEPTED` are positive lifecycle evidence. Unknown,
canceled, declined, expired, countered, invalid, confirmation, escrow, or
other states are not success proof.

## Safety boundaries

- `TradeOfferItemEvidence.assetid` is Trade Offer item-side identity, not the
  final recipient inventory Purchase assetid.
- `SteamTradeOfferLifecycle.ACCEPTED` is not `DeliveryStatus.RECEIVED`.
- `items_to_receive` is not final recipient ownership proof.
- `items_to_give == ()` is not authorization to accept an offer.
- No `Store.advance`, DeliveryStatus mutation, Purchase reconciliation,
  `DeliverySnapshot` mutation, planner change, or coordinator change occurs.
- No `SEND_OFFER`, `ACCEPT_OFFER`, Trade Offer mutation, Steam Guard,
  confirmation, BUFF write, or runtime integration occurs.

Future acceptance must independently bind exact Purchase, exact BUFF order,
exact persisted tradeoffer ID, exact recipient, exact direction, exact
lifecycle, and safe item-side evidence.

## Frozen files and future boundary

The final business diff is limited to:

1. `app/auto_offer/adapters.py`
2. `app/auto_offer/platform_readonly.py`
3. `tests/test_auto_offer_adapters.py`
4. `tests/test_auto_offer_platform_readonly.py`
5. `.agent/AUTO_OFFER_STEAM_TRADE_OFFER_EVIDENCE.md`

TASK-014 is reserved for exact Trade Offer reconciliation and coordinator
routing. After TASK-013 completion, TASK-008 through TASK-012 must be reviewed
for compatibility before TASK-014 planning. Historical tasks must not be
rewritten; compatibility issues require a separate hardening task.
