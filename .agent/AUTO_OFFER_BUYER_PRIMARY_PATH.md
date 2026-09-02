# Auto Offer buyer primary path

This document is the compact current buyer-side architecture baseline after TASK-083 retirement of abandoned recovery experiments.

## Production path

The maintained behavioral reference is `Steamauto/Steamauto@506afd537c34ced23020647c1d411263896c62ed`. AetherSwap independently implements the same practical BUFF surfaces and keeps its own exact-account, Store, and Steam evidence contracts.

Buyer-side orchestration is intentionally centered on the realtime path:

`wait_send_offers -> buyer_send_offer -> steam_trade -> exact Steam Trade Offer -> confirmation/completed-trade/receipt`

- `wait_send_offers` is the source for current buyer-send eligibility.
- `buyer_send_offer` remains one exact BUFF order per Aether invocation.
- No immediate Steam Trade Offer ID is fabricated from the write response.
- A later realtime `steam_trade` record may bind the exact canonical Trade Offer ID.
- Once an ID is bound, the existing exact Steam lifecycle/completed-trade/receipt chain remains authoritative.

TASK-082 owns the exact production state semantics and tests for this path.

## Retired identity-recovery paths

The following are not production architecture and must not be reintroduced without new OWNER/product direction plus current evidence:

- historical BUFF order rows as a durable source of buyer Trade Offer identity;
- Steam Community sent-history HTML before/after delta discovery;
- generic sent-offer candidate binding/enumeration contracts;
- pre-SEND recovery-fingerprint proposals created only to support speculative enumeration;
- TASK040-specific identity archaeology or replay.

The historical TASK040 row remains audit evidence only. It is not a reason to add a second buyer recovery subsystem.

BUFF historical reads remain valid only for independent order lifecycle/refund evidence where still used.
The historical buyer-offer capability is physically absent from the current source.

## Complexity budget

Retain only complexity with a current production purpose:

- exact current account and Steam identity binding;
- exact BUFF order matching;
- Store CAS/stale-revision protection;
- bounded per-order progression;
- seller-send exact ACCEPT;
- exact Steam offer, confirmation, completed-trade and receipt evidence;
- OFF zero intrusion and Host ownership isolation.

Do not add a second worker, scheduler, Store, state machine, discovery subsystem, or historical offer-identity authority.

REAL-WRITE remains CLOSED. Integration to `main` remains OWNER-gated.
