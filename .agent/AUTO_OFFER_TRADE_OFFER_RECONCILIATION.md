# Auto Offer Trade Offer Reconciliation

Historical review of TASK-008 through TASK-012 is PASS. TASK-014 composes only
read-only Trade Offer evidence with the existing reconciliation planner and
single-step coordinator.

For `READ_STEAM_TRADE_OFFER`, the persisted delivery snapshot, request, and
`SteamTradeOfferEvidence` use the same exact `steam_tradeoffer_id`.
`PlatformResult` supplies request-to-evidence cross-binding; the planner also
checks snapshot-to-request equality. There is no coercion, rebinding, latest
offer lookup, partner inference, or timestamp/item-name matching.

`OFFER_CONFIRMED` means exact safe Trade Offer proof, not receipt. `ACTIVE`
confirms an offer but cannot advance from `OFFER_CONFIRMED`; it returns
waiting. `ACCEPTED` advances only the adjacent `OFFER_CONFIRMED` to
`AWAITING_INVENTORY` transition. `ACCEPTED` is never `RECEIVED`.

Seller routes require `is_our_offer=False`; buyer routes require
`is_our_offer=True`. Every positive transition requires `items_to_give == ()`.
An empty give side is not authorization to accept an offer: this task has no
`ACCEPT_OFFER` or `SEND_OFFER` path. Trade Offer item `assetid` values, whether
one or many, are not Purchase asset attribution and cannot set receipt fields.

The coordinator routes seller `OFFER_RECEIVED`, buyer `OFFER_SENT`, and either
mode's `OFFER_CONFIRMED` to `READ_STEAM_TRADE_OFFER`. It copies the exact
persisted Trade Offer ID into that request; old read routes use `None`.
Request comparison includes this field. The Store remains a one-read,
at-most-one-CAS boundary: no loop, retry, polling, platform write, runtime, or
real Steam/BUFF request exists here.

Future work owns Trade History, Trade Receipt, and exact inventory-to-Purchase
asset reconciliation.
