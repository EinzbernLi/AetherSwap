# Exact Steam mobile confirmation foundation — TASK-029

## Scope

TASK-029 adds one isolated Steam mobile-confirmation capability for an already
exact-bound Steam Trade Offer ID.

It does not wire mobile confirmation into the Coordinator, reconciliation,
Store, host lifecycle, pipeline, credential persistence, UI, or workers.

The real-write gate remains closed.

## Why this is separate from `OFFER_CONFIRMED`

The existing delivery status `OFFER_CONFIRMED` predates this task and means that
an exact Steam Trade Offer was positively observed by the read-side lifecycle.
It is not a mobile-authenticator confirmation bit.

TASK-029 does not rename or repurpose that status.

## Clean-room behavioral reference

Behavioral reference:

`Steamauto/Steamauto@e803e1ab00cfcede6ef8a7f1b9e784f9bb8da25a`

Relevant reference files:

- `steampy/confirmation.py`
- `steampy/guard.py`

Only protocol behavior is reused:

- mobile confirmation list;
- confirmation detail lookup;
- Steam Guard identity-secret confirmation key;
- per-confirmation `allow` operation.

The following reference behaviors are deliberately rejected:

- retry/sleep loops;
- creator-id-only fallback;
- suffix/end matching;
- bulk confirmation.

## Contract extension

`PlatformCapability.CONFIRM_OFFER` is explicit and write-side.

`PlatformRequest.steam_tradeoffer_id` is mandatory for this capability.

`ConfirmOfferEvidence` contains only:

- exact `steam_tradeoffer_id`;
- exact authenticated `account_steam_id`.

A successful `PlatformResult` for `CONFIRM_OFFER` must contain that exact typed
identity evidence. Mismatched Trade Offer or account identities are invalid.

`DEFAULT_PLATFORM_CAPABILITIES` is unchanged; confirmation is never enabled by
default.

## Transport ownership

`SteamTradeOfferConfirmationTransport` accepts injected:

- Steam cookie string;
- `identity_secret`;
- HTTP session;
- timeout;
- clock;
- response/list bounds.

It performs no login, refresh, credential persistence, background work, retry,
sleep, polling, or scheduling.

The transport requires a strict `steamLoginSecure` identity and a `sessionid`
cookie. TLS verification may not be disabled.

The `identity_secret` must be strict base64 that decodes to exactly 20 bytes.
Only the decoded bytes are retained. Secret material is never interpolated into
exception messages, result detail, or logging.

## Exact-match preflight

One `confirm(tradeofferid)` call:

1. validates a canonical positive-decimal Trade Offer ID;
2. performs exactly one `/mobileconf/getlist` read;
3. reads at most the configured bounded number of detail pages;
4. extracts only exact HTML IDs of the form `tradeoffer_<full-id>`;
5. requires exactly one confirmation whose full ID equals the requested Trade
   Offer ID;
6. only then sends one `/mobileconf/ajaxop` with `op=allow`.

No list order, “latest”, first item, creator ID, suffix, substring, or fuzzy
match can authorize the mutation.

If zero or multiple exact matches exist, no mutation is sent.

## Non-idempotent write semantics

Although Steam exposes `/mobileconf/ajaxop` as an HTTP GET, TASK-029 treats
`op=allow` as a non-idempotent platform mutation.

After that request is dispatched, all of the following are unproven write
outcomes:

- timeout;
- network exception;
- non-200 response;
- oversized response;
- malformed JSON;
- non-mapping JSON;
- response without exact `success is True`.

All map to `SteamConfirmationWriteResultUnknown`, which the adapter normalizes
to `PlatformResultStatus.RESULT_UNKNOWN`.

There is no automatic resend.

## Adapter

`SteamTradeOfferConfirmationAdapter` declares exactly:

`{PlatformCapability.CONFIRM_OFFER}`

Construction binds one configured host account ID and one exact recipient Steam
ID to a transport whose cookie-bound Steam ID must match.

Request identity mismatch is rejected before the transport call.

Only a returned mapping with both exact requested Trade Offer ID and exact
recipient Steam ID may become `ConfirmOfferEvidence`.

Preflight/read errors are distinguishable from post-dispatch write ambiguity,
but neither can manufacture success.

## Runtime isolation

TASK-029 intentionally does not add `CONFIRM_OFFER` to:

- `DeliveryCoordinator` write routing;
- `_READ_CAPABILITIES`;
- Coordinator tradeoffer-bound capability sets;
- reconciliation;
- host adapter registry;
- runtime factories.

A Coordinator registry containing `CONFIRM_OFFER` therefore remains rejected in
TASK-029.

This is deliberate. A later task must first define:

- exact read evidence for Steam confirmation-required lifecycle;
- durable confirmation-attempt state;
- recovery behavior after unknown confirmation result;
- identity-secret credential ownership.

## Credential deferral

Current AetherSwap Steam credentials persist cookies/session/account identity
only. TASK-029 does not silently add `identity_secret` to persistent config,
imports, routes, or UI.

Until a later credential task, the secret is constructor-injected only.

## Verification requirements

All tests must use fake/injected HTTP behavior.

Required proofs include:

- no mutation without one unique exact full-ID match;
- no creator-ID-only or suffix fallback;
- no bulk confirmation;
- at most one `ajaxop` call;
- write ambiguity always becomes `RESULT_UNKNOWN`;
- no retry/sleep/polling;
- strict identity and TLS validation;
- secret-safe errors/results;
- exact typed success evidence;
- Coordinator remains unwired;
- historical Auto Offer/host/full-suite regression remains green.

## Real-write gate

CLOSED.

TASK-029 authorizes no real Steam or BUFF request, including read-only
`/mobileconf/getlist` or `/mobileconf/details/*`.

A real confirmation can occur only after later runtime/credential work and a
separate OWNER-authorized canary.
