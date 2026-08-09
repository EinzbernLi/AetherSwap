# TASK-018 — Strict Steam Completed Trade Read Transport

Architecture: `native-auto-offer-module-v1`

Post-TASK-017 Historical Review: **PASS**.

TASK-018 supplies only the concrete read-side transport required by the merged TASK-016 `SteamCompletedTradeReader` protocol. It does not register the reader at runtime and it does not alter Store, planner, coordinator, state transitions, Pipeline, workers, or host Purchase state.

## Clean-room reference hierarchy

The contract is designed from the public Steam/Steamworks behavior first. `IEconService.GetTradeOffer` accepts an exact `tradeofferid`. `GetTradeHistory` is deliberately not used: Valve documents it as a publisher-key / secure-server API and its paginated search surface is unnecessary for an already bound Trade Offer.

Behavior-only references used during design:

- `Steamauto/Steamauto @ e803e1ab00cfcede6ef8a7f1b9e784f9bb8da25a`
- inspected upstream `steampy` behavior

No code is copied or vendored from either project and neither becomes a runtime dependency. The relevant observed behavior is only the proof chain: an exact accepted Trade Offer exposes its `tradeid`; that `tradeid`, not `tradeofferid`, identifies the Community receipt.

## Exact proof chain

The reader implements exactly:

`steam_tradeoffer_id`
→ exact `IEconService/GetTradeOffer/v1/`
→ accepted offer
→ exact `offer.tradeid`
→ exact `/trade/{tradeid}/receipt`
→ exact source item identity to `new_contextid/new_assetid`
→ exact recipient inventory identity
→ TASK-016 normalized completed-trade mapping.

It never scans `GetTradeOffers`, never calls `GetTradeHistory`, never chooses latest/newest/nearest records, and never matches by name, price, classid, instanceid, list position, or time proximity.

`tradeid` and `tradeofferid` are distinct identities. A missing `tradeid` means completion is not proven and the reader returns no evidence rather than substituting the Trade Offer ID.

## Credential boundary

The constructor receives only an already-owned Steam cookie string plus optional injected HTTP transport and fixed read bounds. It performs zero network I/O.

`steamLoginSecure` is mandatory. Only canonical `steamid||access_token` or `%7C%7C` separation is accepted. The embedded SteamID64 binds the reader to one account; every call must provide that exact recipient SteamID before network I/O is allowed.

The module does not read config files, passwords, shared secrets, identity secrets, Steam Guard data, or mobile-confirmation data. It does not login, relogin, refresh credentials, or persist cookies/tokens. Credentials and raw authenticated response bodies are never included in repr or raised error text.

## Exact accepted offer

The first read is the hard-coded Steam `GetTradeOffer` endpoint with only the authenticated web access token, exact requested `tradeofferid`, and language parameter.

The returned exact offer must bind back to the requested Trade Offer ID and must be in Steam Accepted state. `accountid_other` is deterministically converted to counterparty SteamID64. `time_updated` is the evidence `completed_at`; local wall-clock time is never substituted.

`items_to_give` and `items_to_receive` are normalized only from exact `(appid, contextid, assetid, amount)` source identities. Duplicate source identities fail closed. Offer-side direction is not heuristically inverted.

## Receipt parsing and asset mapping

The Community receipt is fetched only at `/trade/{tradeid}/receipt` with the already-owned Community cookies.

The parser is an independent bounded, quote/escape-aware balanced-object scanner for `oItem = {...}` JSON objects. It does not copy the behavior-reference parser, does not use a greedy regex, and enforces receipt body and object-count limits.

Each source item must have exactly one receipt object with the same `(appid, contextid, assetid, amount)` plus exact `new_contextid` and `new_assetid`. Missing proof returns no evidence; duplicate or partially populated mappings fail closed. `new_assetid == assetid` remains valid because Steam is not assumed to always re-key an asset.

## Recipient inventory confirmation

Only the exact recipient inventory can confirm final ownership. For the one TASK-017-eligible received item, the reader queries only:

`/inventory/{recipient_steam_id}/{appid}/{new_contextid}`

and requires the exact `(appid, new_contextid, new_assetid, amount)` identity.

Pagination is deterministic endpoint pagination only: fixed page bound, monotonic unique `start_assetid`, no repeated cursor, no sleep, retry, polling, or backoff. A readable completed inventory scan that lacks the exact item returns an empty confirmation set rather than fabricated evidence.

If the completed trade already has outgoing items or multiple received items, inventory confirmation may be skipped because TASK-017 will block those shapes before RECEIVED attribution. This avoids unnecessary authenticated reads without weakening fail-closed behavior.

## Network safety

The module has a fixed three-route allowlist by construction:

1. exact Steam API `GetTradeOffer` GET;
2. exact Steam Community `/trade/{tradeid}/receipt` GET;
3. exact Steam Community `/inventory/{steamid}/{appid}/{contextid}` GET.

All operations are GET-only, use finite connect/read timeouts, and disable redirect following. TLS verification is never disabled; an injected session explicitly configured with `verify=False` is rejected. 401/403/redirect outcomes are sanitized authentication failures. Timeouts use the existing `PlatformAdapterTimeoutError` path. Other failures use sanitized messages without URLs, tokens, cookies, or raw bodies.

There is no retry loop, sleep, ThreadPool/background work, POST, PUT, PATCH, DELETE, send/accept/decline/cancel/counter offer behavior, confirmation, market mutation, BUFF write, or inventory mutation.

## State-machine boundary

The normalized mapping is intended to be passed unchanged to the merged `SteamCompletedTradeReadOnlyAdapter`, which remains responsible for typed TASK-016 evidence validation and exact tradeoffer/account cross-binding.

Transport success is **not** state-transition authority. TASK-017 remains the sole planner allowed to decide `AWAITING_INVENTORY -> RECEIVED`, including its zero-outgoing, single-received-item, exact-inventory-confirmation and contract gates.

No runtime registration occurs in TASK-018. Runtime/session construction and adapter registration are eligible only after TASK-018 merge, post-merge CI, and mandatory Historical Review PASS.
