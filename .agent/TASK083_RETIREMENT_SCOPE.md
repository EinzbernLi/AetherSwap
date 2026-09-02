# TASK-083 — retired buyer recovery artifact cleanup

Status: implementation in progress on `task/083-remove-retired-recovery-artifacts`.

Base: `integration/auto-buyer-offer@aa6c19ff596e764d8951f726ae3ae1574eb5d4b8`.

## Goal

Physically remove buyer-offer identity discovery experiments that are no longer part of the product direction, so future source review defaults to the realtime Steamauto-parity path instead of speculative recovery archaeology.

## Delete as a closed dependency island

Production experiment modules:
- `app/auto_offer/sent_offer_binding.py`
- `app/auto_offer/steam_community_sent_history.py`
- `app/auto_offer/steam_community_sent_history_requests.py`
- `app/auto_offer/steam_community_sent_history_transport.py`

Experiment/probe tests:
- `tests/test_auto_offer_sent_offer_binding.py`
- `tests/test_auto_offer_steam_community_sent_history.py`
- `tests/test_auto_offer_steam_community_sent_history_requests.py`
- `tests/test_auto_offer_steam_community_sent_history_transport.py`
- `tests/sent_history_probe_outcome.py`
- `tests/test_sent_history_probe_outcome.py`

Superseded architecture document:
- `.agent/AUTO_OFFER_RESULT_UNKNOWN_RECOVERY.md`

Add compact current baseline:
- `.agent/AUTO_OFFER_BUYER_PRIMARY_PATH.md`

## Explicitly retained

Do not remove or weaken:
- historical BUFF reads used for refund/order lifecycle evidence;
- exact Steam `GetTradeOffer`, completed-trade, receipt, inventory, confirmation paths;
- seller-send exact ACCEPT;
- Store CAS and Host ownership boundaries;
- TASK-069 persisted account-lineage compatibility;
- recovery-only maintenance for already-bound exact identities;
- canary authority/gates.

TASK-083 does not change buyer runtime state semantics. TASK-082/#233 owns the subsequent Steamauto-parity behavior change and embedded TASK-056 capability removal.

No real platform request/write is authorized. REAL-WRITE remains CLOSED.
