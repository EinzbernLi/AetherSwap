# Auto Offer Single-Target Canary Isolation Contract — TASK-036 provenance repair

TASK-036 is a safety-boundary task for a **future** first live Auto Offer canary. It does not authorize live credential access, a real Steam/BUFF/mobile-confirmation mutation, or merge to `main`.

This revision closes both the same-process owner-context self-mint bypass reported by Terra High Max in Issue #92 comment `5267532563` and the caller-selected production authority/root partition bypass reported in comment `5274521923`, while preserving the earlier TOCTOU, generation/replay, competing-writer, and exact-target protections.

## Guarantee boundary

Within **current-version AetherSwap processes running under the same OS user on the same host**:

- one OS-user-scoped lock namespace is shared across installation directories;
- every public production Host Auto Offer activation obtains that namespace only from `get_canary_authority()`; caller-selected authority/root injection is rejected before identity, bridge, session, or adapter construction;
- at most one live canary generation owns that namespace;
- only the one target Host integration that successfully creates and privately retains the opaque owner session may execute canary owner writes;
- exact Auto Offer SEND/CONFIRM remain bound to one permit target;
- covered non-canary writers fail closed while a canary is active or crash-stale;
- owner death releases the live OS lock but leaves durable metadata as a stale fence.

This guarantee does **not** include another host/VM/container/OS user, an old or unpatched binary, Steam/BUFF browser/client/mobile/manual actions, third-party bots/scripts/APIs, or arbitrary hostile same-process Python code using reflection/`ctypes`. Those remain future live-preflight abort conditions or outside the current-version component threat model.

## Permit is identity, not authority

`CanaryPermit` remains immutable, serializable, and secret-free. It binds exactly:

- permit ID and non-secret owner nonce;
- Host Purchase primary key;
- BUFF order ID;
- deterministic `purchase_id = buff:<order_id>`;
- Auto Offer account ID;
- recipient SteamID;
- expected one-element Host pending set;
- expected Store existence/revision/status/Trade Offer binding;
- creation time.

Permit data, generation data, durable metadata, the public authority singleton, and exact target tuples are **not execution capabilities**.

No cookie, `steamLoginSecure`, session ID, `identity_secret`, shared secret, access/refresh/authorization token, confirmation signature/nonce, raw buyer info, or HTTP body/header is stored in permit/authority metadata.

## Opaque owner session provenance

Successful internal Host activation creates one process-local `_CanaryOwnerSession` backed by an object-identity capability that is never persisted.

Properties of that session:

- private implementation type and excluded from `__all__`;
- fixed opaque `repr` that contains no permit/capability material;
- no `__dict__` capability export surface;
- pickle/copy serialization through reduction is rejected;
- retained only by the target `HostAutoOfferIntegration` construction path and the Coordinator write-guard closure it owns;
- not returned by `get_canary_authority()`, metadata APIs, or permit APIs.

The authority's public compatibility surfaces cannot mint owner execution:

- public `arm(permit)` is fail-closed;
- public `recover_and_rearm(...)` is fail-closed;
- public `owner_runtime_guard(permit)` is fail-closed;
- public `mark_completed()` and `release_keep_fence()` are fail-closed while owner-session methods perform those operations;
- public `external_write_guard(exact_target)` cannot become an Auto Offer owner write merely because the same permit values are known.

Production Host activation has an additional provenance rule: both exported `build_host_auto_offer_integration(...)` and direct `HostAutoOfferIntegration(...)` construction use only the module production authority returned by `get_canary_authority()`. Their retained compatibility keyword `canary_authority` is not an execution seam; any non-`None` value fails closed. The public factory performs that rejection before account identity lookup, bridge construction, owner-session minting, or adapter access. Arbitrary-root `CanaryAuthority` instances remain direct authority/unit-test seams only; tests that need one may monkeypatch the module production singleton, but no caller-selected root can be supplied through a production Host activation API.

A second/alternate Host integration supplied only the same public permit and authority values, but not the original opaque session, is rejected.

## No ambient thread privilege

Owner runtime serialization does **not** install a thread-local "owner" role. It only holds the process-local `RLock` around target Host progression.

Exact owner SEND/CONFIRM/receipt guards require the opaque session on every final boundary call. Therefore callback or re-entrant code running on the same thread cannot piggyback owner authority through public guards.

During an active owner SEND/CONFIRM, public re-entrant writes are rejected. Owner-session re-entry is also rejected.

The sole narrow nested exception is the existing local Host receipt primitive: the outer opaque-session `host_receipt` guard may admit exactly one public `State.complete_purchase_receipt_by_id(...)` refinement carrying the same BUFF order ID, Host DB ID, and exact asset ID. It cannot refine into SEND/CONFIRM or another asset and cannot be consumed twice in the same owner write.

## One-shot generation and crash recovery

Durable metadata carries:

- monotonically increasing generation;
- phase `armed`, `completed`, or `retired`;
- bounded `used_permit_ids` ledger.

Consumed permit IDs cannot be reused.

`clear_stale()` remains disabled. Internal Host recovery rotates a crash-stale active generation only while continuously holding the same OS lock:

1. acquire the same user-scoped OS lock nonblocking;
2. verify the exact old active permit/generation;
3. reject a used permit ID;
4. atomically replace metadata with a fresh generation;
5. retain the same OS lock and create a fresh in-process owner session.

There is no expiry or time-based lease stealing. An armed stale generation cannot be retired directly. Only a proven durable `completed` generation may later be retired while preserving permit history.

## Pure preflight

`preflight_canary_permit(...)` remains snapshot-only and structurally write-free. It accepts no Store mutation API, adapter, transport, receipt writer, payment writer, or network client.

It fails closed unless:

- exactly one target Host pending Purchase is supplied;
- no unrelated recoverable/nonterminal Auto Offer row exists;
- target Host/Store identities match exactly;
- expected Store absence or exact revision/status/binding matches;
- `unresolved_checkout is None`;
- no pre-existing Trade Offer ambiguity exists for a first SEND.

The normal Host gate is not a preflight API.

## Final Host TOCTOU barrier

Pipeline snapshots never authorize SEND/CONFIRM by themselves.

Immediately before exact canary SEND or CONFIRM, the opaque-session write guard opens the existing Host `config/app.db`, executes `BEGIN IMMEDIATE`, re-reads `purchase WHERE pending_receipt = 1`, and requires exactly one live row matching the permit Host DB ID and BUFF order with no received asset.

The SQLite write exclusion remains held across the external SEND/CONFIRM adapter body and is rolled back/released afterward. A second Host pending row introduced after an earlier snapshot therefore blocks before adapter execution.

Completion also refuses while any pending Host Purchase remains.

No Host DB schema/table/column migration is introduced.

## Existing write fences preserved

During active or crash-stale canary, current-version paths remain fenced before irreversible mutation:

- BUFF purchase/payment;
- generic Host Purchase/Sale mutation;
- legacy Steam receive accept;
- Steam market listing;
- Steam market delist `removelisting` POST;
- legacy bulk mobile confirmation / `accept_all`;
- Steam gift cart mutations;
- Steam gift checkout init/finalize.

Normal gift flow still holds one normal-writer slot across cart-clear/add/modify/checkout, and each gift mutation POST retains its final-boundary fence.

## Auto Offer durable write semantics preserved

`DeliveryCoordinator` remains the only active Auto Offer platform-step/state-transition authority.

- `AWAITING_OFFER -> OFFER_ATTEMPTED` is persisted before SEND guard/adapter;
- `OFFER_CONFIRMATION_REQUIRED -> OFFER_CONFIRMATION_ATTEMPTED` is persisted before CONFIRM guard/adapter;
- persisted attempted/`RESULT_UNKNOWN` states never resend or reconfirm;
- later recovery is exact Steam Trade Offer read only.

The Coordinator's canary write-guard closure is bound to the target Host's opaque owner session, not to the public authority object.

## Terminal behavior

The target `RECEIVED` state may retry only exact idempotent local Host receipt writeback. It must not replay platform writes.

After exact Host receipt closure and no remaining pending Host Purchase, the opaque session may mark the canary generation completed. The owning buy pipeline stops before selecting or paying for another item, and canary `close()` never dispatches fresh work.

## Verification boundary

Verification is fake/injected/local-only. Required gates include:

- self-mint probe using all public permit/generation/metadata/authority surfaces -> owner write body **0**;
- public exact-target guard while owner live -> body **0**;
- alternate Host integration with same permit but no session -> no owner execution;
- production owner active + public factory supplied production authority explicitly -> fail before identity/bridge/write;
- production owner active + public factory supplied each of two distinct-root authorities -> fail before identity/bridge/write, and neither alternate authority becomes owner;
- non-canary public factory with a distinct-root authority -> fail before identity/bridge/write rather than partitioning the active production fence;
- exported direct `HostAutoOfferIntegration` construction with production or distinct-root authority explicitly supplied -> fail closed;
- callback/re-entrant public and owner-session writes -> body **0**;
- opaque session non-serialization and secret-free repr/evidence;
- exact session SEND/CONFIRM still hold Host SQLite final barrier;
- receipt nested refinement is exact and single-use;
- generation replay/crash-stale/atomic rotation proofs;
- historical TASK-028/031/032/034 recovery/write regressions;
- BUFF/receive/sell/delist/gift regressions;
- full Auto Offer and full repository pytest;
- baseline minimum 1438;
- network-interdicted full suite;
- `pip check` and `git diff --check`;
- fresh Luna High Max Windows exact-tree verification;
- fresh Terra High Max adversarial review of the exact repaired canonical.

Only Terra `PASS_TO_PR` may unlock TASK-036 PR creation.

The pre-existing normal-mode gift token-bearing logging LOW from Terra comment `5267532563` is explicitly outside this repair and must not be mixed into the canary authority change.

**REAL-WRITE GATE remains CLOSED.**
