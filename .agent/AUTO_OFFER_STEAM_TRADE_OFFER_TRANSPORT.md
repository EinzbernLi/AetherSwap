# TASK-019 — Strict Steam Trade Offer Read Transport

Architecture: `native-auto-offer-module-v1`

Execution base: `integration/auto-buyer-offer @ 98affcd098b3f7b5bf4ec34d2c21ed0522ade841`

Post-TASK-018 Historical Review: **PASS**.

## Scope

TASK-019 adds only the production-capable exact read-side transport required by the merged TASK-013 `SteamTradeOfferReader` protocol. It does not register runtime adapters, mutate Store state, alter the planner/coordinator, attach to Pipeline/workers, or perform any Steam/BUFF write.

Concrete reader:

`SteamTradeOfferHttpReader(steam_tradeoffer_id: str) -> object`

It reuses the already-reviewed TASK-018 authenticated GET safety boundary rather than introducing a second network stack.

## Exact proof chain

The only positive read chain is:

`exact persisted steam_tradeoffer_id`
→ exact `IEconService/GetTradeOffer/v1/`
→ exact returned `tradeofferid`
→ exact account/counterparty/direction/item facts
→ ACTIVE or ACCEPTED lifecycle
→ TASK-013 normalized mapping
→ existing `SteamTradeOfferReadOnlyAdapter`
→ typed `SteamTradeOfferEvidence`.

The reader does not scan offer lists, history, time windows, names, prices, partners, newest records, or nearest records. It performs no fallback lookup.

## Credential boundary

The constructor receives only an already-owned Steam cookie string, optional injected HTTP session, finite timeout bounds, and JSON size bound. Construction performs zero network I/O.

`steamLoginSecure` remains mandatory and strict. The embedded SteamID64 is the authenticated account identity and is returned as `account_steam_id` in normalized evidence.

The reader does not load configuration, passwords, Steam Guard secrets, confirmation secrets, or credentials files. It does not login, relogin, refresh, persist, or rotate credentials.

TASK-019 deliberately does not reuse historical host helpers whose network policy permits disabled TLS verification, redirects, login/relogin, retry, sleep, or broader session behavior.

## Exact request

A call first validates `steam_tradeoffer_id` as a canonical positive decimal string before any network I/O.

It then performs exactly one authenticated GET to the fixed Steam API Trade Offer endpoint with:

- `access_token` from strict `steamLoginSecure`;
- exact requested `tradeofferid`;
- `language=english`.

No API key is required. No Community cookie is sent to this API request. Redirects are disabled and timeouts are finite.

## Returned offer binding

The response must contain one mapping at `response.offer`.

The returned `tradeofferid` must exactly equal the requested ID. The reader strictly validates:

- `trade_offer_state` as a non-bool integer;
- `accountid_other` as a valid Steam account ID and maps it deterministically to SteamID64;
- `is_our_offer` as a real bool;
- `items_to_give` as a list;
- `items_to_receive` as a list.

The counterparty SteamID64 must differ from the authenticated account SteamID64.

## Lifecycle

Only two numeric Steam states become positive TASK-013 evidence:

- `2` → `active`;
- `3` → `accepted`.

Any other structurally valid integer lifecycle returns no positive evidence. Malformed lifecycle type or malformed offer shape fails closed.

The lifecycle reader intentionally does not require `tradeid`, receipt HTML, `time_updated`, or inventory proof. Those belong to the separate TASK-018 completed-trade receipt transport.

ACTIVE or ACCEPTED is still not receipt proof and is never equivalent to `DeliveryStatus.RECEIVED`.

## Item evidence

Both sides normalize only exact source item identity:

`(appid, contextid, assetid, amount)`.

Each field is strict; duplicate `(appid, contextid, assetid)` identities on one side fail closed. Output ordering is deterministic. One side may be empty, but both sides may not be empty.

Names, market names, class/instance metadata, prices, descriptions, icons, and list position are discarded and never used for identity.

## Network safety

TASK-019 inherits the TASK-018 hardened transport rules:

- GET only;
- exact URL allowlist;
- TLS verification preserved;
- injected sessions with observable `verify=False` rejected;
- redirects disabled;
- finite connect/read timeout;
- bounded JSON body;
- 401/403/redirect outcomes sanitized;
- timeout normalized through `PlatformAdapterTimeoutError`;
- generic network failures sanitized;
- no raw cookies, token values, response bodies, or exception text in raised errors;
- no retry, polling, sleep, thread, or background work.

## Adapter and planner boundary

The normalized mapping contains exactly:

- `steam_tradeoffer_id`;
- `account_steam_id`;
- `counterparty_steam_id`;
- `is_our_offer`;
- `lifecycle`;
- `items_to_give`;
- `items_to_receive`.

The existing TASK-013 adapter remains responsible for typed evidence construction and request/evidence cross-binding.

The existing TASK-014 planner remains the only authority for lifecycle state proposals and preserves its frozen gates:

- exact Trade Offer binding;
- exact delivery direction;
- outgoing item safety;
- adjacent-only transitions;
- no Store write from the reader or adapter.

TASK-019 does not modify `platform_readonly.py`, `reconciliation.py`, `coordinator.py`, contracts, Store, Pipeline, workers, account/config loading, or any host runtime surface.

## TASK-018 regression boundary

`SteamCompletedTradeHttpReader` remains unchanged in observable behavior. TASK-019 must preserve all TASK-018 tests for:

- strict cookie parsing;
- exact accepted Trade Offer to exact `tradeid` receipt;
- bounded receipt parsing;
- exact post-trade asset mapping;
- exact inventory confirmation;
- pagination limits;
- URL/TLS/redirect/timeout/error safety.

## Runtime boundary

Runtime construction/registration remains deferred to TASK-020. TASK-019 creates no singleton, registry mutation, startup hook, Pipeline hook, worker hook, config toggle, or default enablement.

`AUTO_OFFER_DEFAULT_ENABLED = False` remains unchanged.

## Write boundary

TASK-019 performs no:

- BUFF write;
- Steam write;
- Trade Offer create/send/accept/decline/cancel/counter;
- Steam Guard action;
- mobile confirmation;
- inventory mutation;
- Purchase mutation;
- Store mutation.

Write-side SEND_OFFER, ACCEPT_OFFER, and confirmation work remain separate future tasks requiring their own idempotence, `result_unknown`, exact identity, duplicate-prevention, fail-closed, canary, and OWNER authorization contracts.

## Historical Review Gate

After TASK-019 merge and post-merge CI, review TASK-013 through TASK-019 plus `result_unknown`, Store CAS, default-off, and import/runtime side-effect boundaries before freezing TASK-020.
