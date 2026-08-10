# Auto Offer BUFF buyer-send transport

TASK-025 introduces an isolated production-capable BUFF buyer `SEND_OFFER`
transport without wiring it into any runtime, host lifecycle, Pipeline, worker,
or scheduler.

## Authority

The runtime write authority remains the TASK-024 chain:

`PlatformAdapter -> DeliveryCoordinator -> AutoOfferStore CAS`

This task adds only a transport and adapter implementation. It does not add a
second coordinator, executor, planner, journal, Store, worker, or retry loop.

TASK-007 `DeliveryExecutor` remains a historical compatibility/test abstraction
and is not part of this write path.

## Frozen external behavior

Clean-room behavioral reference:

- repository: `Steamauto/Steamauto`
- exact reference: `e803e1ab00cfcede6ef8a7f1b9e784f9bb8da25a`
- endpoint: `POST /api/market/manual_plus/buyer_send_offer`
- JSON keys: `buyer_info`, `bill_orders`, `steamid`
- `bill_orders` is a list
- `buyer_info` is an encrypted Steam cookie string

AetherSwap intentionally narrows the request to one exact order per invocation.

The reference's generic POST retry behavior is not copied.

## Encryption envelope

The interoperable envelope is implemented independently using PyCryptodome
standard primitives:

1. generate a random 16-byte AES key;
2. generate a random 16-byte IV;
3. AES-CBC encrypt UTF-8 Steam cookie text with PKCS#7 padding;
4. RSA PKCS#1 v1.5 encrypt the AES key using the frozen BUFF-compatible public
   key;
5. concatenate encrypted AES key + IV + ciphertext;
6. base64 encode the result.

Steam cookie plaintext is never logged, persisted, added to result evidence, or
included in exception messages.

## Session and request ownership

`BuffBuyerSendTransport` wraps an already-owned BUFF client. It does not create
a `requests.Session`, request policy, credential store, or account selection.

The actual POST is delegated exactly once to the existing hardened
`BuffBuyer._make_request`, preserving:

- request-slot serialization;
- current session cookies;
- CSRF refresh;
- Origin/timezone headers;
- risk/rate-limit circuit behavior;
- write timeout/network/5xx/HTML/redirect ambiguity -> `BuffWriteResultUnknown`;
- no automatic write retry.

## Identity

Every invocation is single-order and exact:

- one canonical `buff_order_id`;
- one canonical positive decimal recipient SteamID;
- one strict Steam cookie string containing exactly one non-empty
  `steamLoginSecure`;
- `steamLoginSecure` must encode the same exact SteamID using the historical
  `steamid||token` or `steamid%7C%7Ctoken` shape;
- if the wrapped BUFF client already has a bound SteamID, it must also exactly
  match.

Thus the write transport independently rechecks the Steam-cookie identity leg
before encryption or network I/O instead of trusting a future host provider to
bind it correctly.

The Auto Offer adapter additionally binds request `account_id` and
`recipient_steam_id` to its construction identity before requesting Steam
cookies or invoking the transport.

## Result semantics

TASK-025 deliberately never returns SEND_OFFER success evidence.

The frozen external reference proves only action-level `code == "OK"` and does
not freeze a trustworthy immediate response field containing the new Steam
Trade Offer ID.

Therefore:

- `code == "OK"` -> `RESULT_UNKNOWN / offer_created_unproven`;
- `BuffWriteResultUnknown` -> `RESULT_UNKNOWN / write_result_unknown`;
- other post-invocation responses or ambiguous exceptions -> fail closed;
- no `SendOfferEvidence` is manufactured.

This is compatible with TASK-024: once `OFFER_ATTEMPTED` is durable, any
non-proven invoked result remains non-resendable.

## No runtime wiring

TASK-025 does not modify readonly runtime, host bridge, host integration,
Pipeline, workers, receive flow, or Steam code. Existing application execution
therefore still performs zero Auto Offer buyer-send writes.

No real BUFF/Steam credentials or live requests are used in TASK-025 tests.

## Future boundary

Before any real canary or host write wiring, a separate task must implement
read-only reconciliation for `OFFER_ATTEMPTED` / `RESULT_UNKNOWN` and prove an
exact Steam Trade Offer ID from exact BUFF/Steam evidence.

A real one-order canary remains a separate OWNER gate.
