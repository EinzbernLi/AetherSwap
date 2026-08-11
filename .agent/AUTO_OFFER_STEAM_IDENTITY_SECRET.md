# Steam identity_secret ownership hardening — TASK-030

## Scope

TASK-030 establishes one persistent owner for the Steam mobile-confirmation
`identity_secret` without wiring mobile confirmation into Auto Offer runtime.

Canonical persistent owner:

`config/credentials.json -> steam.identity_secret`

The historical app-config field remains accepted only as migration/update input.
It is never persisted in `app_config.json` after this task.

## Why this task is isolated

Source review found an active historical Steam listing-confirmation path:

- `app/sell_pipeline.py` reads `cfg.steam_confirm.identity_secret`;
- `app/steam_confirm.py` performs the historical listing confirmation behavior;
- the settings page historically received the plaintext secret from `/api/config`.

TASK-030 does not combine that compatibility migration with new Auto Offer
confirmation lifecycle states. Exact Steam `CreatedNeedsConfirmation` evidence
and durable confirmation-attempt recovery are deferred to the next task.

## Canonical validation

Every persisted canonical value must:

- be a non-empty string;
- have no leading/trailing or embedded whitespace;
- be strict Base64;
- decode to exactly 20 bytes.

Validation errors are fixed sanitized codes only:

- `steam_identity_secret_invalid`
- `steam_identity_secret_conflict`

Secret material must never appear in exception text.

`save_credentials()` validates the canonical field, so full credential import
cannot bypass the contract. Steam cookie/session refresh preserves and validates
an existing canonical secret instead of deleting it.

## One-way legacy migration

`load_app_config()` owns the compatibility migration under the existing app
config lock, with the credential write occurring before the app-config scrub.

For a persisted legacy `steam_confirm.identity_secret`:

1. legacy-only valid value -> write canonical credentials, then remove legacy;
2. equal canonical + legacy -> remove legacy only;
3. conflicting canonical + legacy -> fail closed with no mutation;
4. malformed legacy -> fail closed with no mutation;
5. empty legacy field -> remove the empty field and preserve canonical value.

If canonical persistence succeeds but app-config scrub is interrupted, the next
migration sees two equal values and safely finishes the scrub. The inverse
ordering is forbidden because it could delete the only secret before canonical
persistence succeeds.

## Explicit settings updates

The legacy-form app-config field is still accepted as an explicit settings input
for compatibility. A valid non-empty input replaces the canonical credential
first, then app config is written without the secret.

An empty input does not clear the canonical credential.

This allows the existing settings/wizard surface to keep working without
creating a second persistent owner.

## Runtime compatibility alias

`load_app_config_validated()` keeps the historical in-process field available to
existing sell code, but derives it fresh from the canonical credential store.
The cached app-config base contains only an empty placeholder; secret bytes are
not stored in the config cache.

This runtime alias is not credential ownership. It is a compatibility view and
is never written back to `app_config.json`.

## Browser/API boundary

`GET /api/config` replaces a configured secret with the fixed non-secret mask:

`********`

The plaintext secret is never returned to the browser.

When the existing settings form POSTs that exact mask back, the route removes
the field before config update, meaning "preserve existing". Any other non-empty
value is treated as a new candidate and must pass canonical validation.

Responses after config save are masked again.

## Full backup/import

Full export triggers the normal raw app-config migration/scrub and therefore:

- `app_config` contains no `identity_secret`;
- `credentials.steam.identity_secret` contains the canonical secret when one is
  configured.

Full import preflights dual input:

- equal legacy/canonical values are allowed;
- a legacy and canonical value that differ are rejected before mutation.

Credentials are applied before app config. A legacy-only imported app config can
therefore populate the canonical owner after credential replacement. Existing
full-import rollback remains the recovery authority if a later write fails.

## Historical listing behavior

TASK-030 does not edit `app/sell_pipeline.py` or `app/steam_confirm.py`.
The sell pipeline continues to read `cfg.steam_confirm.identity_secret`; that
runtime value is now derived from credentials instead of app-config persistence.

The historical confirmer currently has broader behavior, including bulk
confirmation. TASK-030 neither reuses nor expands that code for Auto Offer.
Auto Offer remains bound to the isolated exact-ID TASK-029 transport.

## Auto Offer isolation

No file under `app/auto_offer/` changes in TASK-030.

In particular:

- `CONFIRM_OFFER` remains not Coordinator-wired;
- there is no confirmation-attempt state yet;
- there is no new Store transition;
- there is no host/pipeline confirmation dispatch;
- there is no real mobile confirmation request.

## Deferred

The next confirmation prerequisite task owns:

- exact Steam `CreatedNeedsConfirmation` read evidence;
- durable confirmation-attempt state;
- confirmation-specific `RESULT_UNKNOWN` read recovery;
- no automatic resend.

A later separately gated task may wire the already-reviewed TASK-029 exact
confirmation adapter only after those durable contracts exist.

## Real-write gate

CLOSED.

TASK-030 performs credential/config migration only. Verification must use local
files and monkeypatched/injected behavior. No real Steam or BUFF request/write is
authorized.
