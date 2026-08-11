# Auto Offer Host Source Preservation — GOV-002

## Highest principle

Preserve upstream host source by default.

New Auto Offer-specific contracts, adapters, orchestration, reconciliation, persistence, transports, confirmation logic, and other feature-specific behavior SHOULD live under `app/auto_offer/**` unless safe correctness requires a host-owned lifecycle or shared platform primitive.

This rule is a future change gate. It is not a mandate to mechanically refactor already accepted TASKs.

## Host-file modification gate

Any future TASK that proposes modifying an existing production file outside `app/auto_offer/**` MUST explain, before source scope is frozen:

1. why that exact host file must change now;
2. why the requirement cannot safely be implemented inside `app/auto_offer/**`;
3. why the proposed seam is narrower and safer than introducing a parallel owner or framework;
4. how the touchpoint should be handled when absorbing future upstream changes, including whether it can later be removed in favor of an upstream public seam.

If a material host-file modification cannot satisfy that justification, the TASK is `SCOPE_BLOCKED` until the OWNER explicitly expands scope.

## Safety over cosmetic zero-touch

Zero host-file edits is not an objective by itself.

A minimal host change is allowed when the host is already the authoritative owner of the relevant lifecycle, configuration transaction, credential store, authenticated platform client, lock, session, cookie rotation, or purchase persistence boundary, and moving that ownership into Auto Offer would create one or more of:

- private-field or private-method reach-through;
- duplicate authentication/request policy;
- duplicate credential ownership;
- duplicate Store, coordinator, state machine, lifecycle manager, worker, scheduler, or platform client;
- start/toggle or other lifecycle races;
- split authority over a non-idempotent write;
- behavior that is harder to remove or rebase when upstream provides an equivalent public seam.

Do not create a generic event bus, plugin framework, second lifecycle manager, or similar abstraction merely to claim cosmetic host isolation.

## Completed TASK policy

Completed and accepted TASKs are not retroactively defects merely because they touched host files.

Revisit a historical touchpoint only when there is concrete evidence of at least one of:

- upstream merge conflict or replacement public API;
- safety defect;
- duplicated authority or ownership;
- a host modification that can now be removed without weakening safety or historical behavior;
- current scope that materially depends on changing that touchpoint again.

Speculative cleanup alone is insufficient reason for refactoring.

## Historical behavior boundary

Historical behavior outside Auto Offer remains out of scope unless the current TASK depends on changing it for safe correctness.

Independent historical security or maintenance debt remains independently tracked. A nearby problem is not permission to broaden an Auto Offer TASK.

## Host Touchpoint Ledger

The following paths receive heightened upstream-review attention. The ledger is an index, not a requirement that every path remain modified forever.

- `app/config_schema.py` — Auto Offer feature default/schema seam.
- `app/pipeline.py` — host lifecycle, purchase gate, and run-level config snapshot seam.
- `app/routes/config.py` — host control-plane/config/credential API seam.
- `config/__init__.py` — canonical shared Steam credential ownership.
- `app/config_loader.py` — validated config/credential compatibility and migration seam.
- `buff/buyer.py` — narrow BUFF buyer read primitive.
- `buff/buyer_send.py` — isolated BUFF buyer-send transport; its existing raw `BuffBuyer._make_request` dependency remains explicit debt and is not a reason for premature refactoring.
- `app/services/buff_client.py` — narrow authenticated facade preserving host authentication lock, client lock, session, credential generation, and cookie-rotation ownership.

When upstream changes one of these paths, review the exact Auto Offer seam first before attempting broader conflict resolution.

## Required TASK freeze question

Every future TASK source whitelist must answer explicitly:

> Why must each modified host production file change, and why can the requirement not be implemented safely inside `app/auto_offer/**` instead?

If the answer is only convenience, aesthetics, speculative extensibility, or future-proofing without a current requirement, remove the host file from scope or mark the TASK `SCOPE_BLOCKED`.

## Existing authority rules remain unchanged

This governance rule does not replace existing Auto Offer safety ownership:

- runtime platform-step authority remains the existing Coordinator/Store path;
- no second executor, planner, Store, state machine, worker, scheduler, credential owner, or platform-client owner;
- non-idempotent writes remain fail-closed and crash-safe;
- `main` remains separately OWNER-gated;
- real Steam/BUFF writes remain separately OWNER-gated.

## Verification policy

GOV-002 changes documentation only. GitHub-native exact diff review and repository CI are sufficient; a separate Luna Windows verification is not required because no production or test bytes change.

`REAL_WRITE_GATE: CLOSED`
`MAIN: UNTOUCHED`
