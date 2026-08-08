# Auto Offer Platform Evidence Contract

TASK-010 extends the pure Auto Offer platform boundary with immutable,
normalized, read-only evidence. It introduces three evidence values:

- `DeliveryDirectionEvidence` proves only `seller_sends_offer`.
- `OfferStateEvidence` holds one exact `steam_tradeoffer_id`.
- `InventoryStateEvidence` holds only canonical recipient-side `assetids` and
  an optional inventory count.

`PlatformResult.request` remains the sole purchase identity binding. Evidence
does not contain, infer, or match names, goods IDs, time proximity, list
position, generic IDs, raw responses, prices, credentials, cookies, tokens,
sessions, HTML, or exception text.

## Result rules

Only successful read capabilities may contain evidence, and their types must
match exactly:

- `READ_DELIVERY_DIRECTION` -> `DeliveryDirectionEvidence`
- `READ_OFFER_STATE` -> `OfferStateEvidence`
- `READ_INVENTORY_STATE` -> `InventoryStateEvidence`

All non-success results have `evidence=None`. A bare success, a wrong evidence
type, or any `SEND_OFFER` success is rejected at the contract boundary.
`FakePlatformAdapter` also maps a configured bare success to malformed with
`success_evidence_required`.

## Read-only adapters

The BUFF adapter adds evidence only after exact bound account, canonical order,
recipient, and platform-state proof. The Steam inventory adapter extracts only
strict, unique asset IDs from a valid success envelope, in sorted tuple order.
Malformed assets, duplicates, missing positive-inventory assets, and count
contradictions are malformed results.

Inventory evidence proves only that an exact recipient inventory snapshot was
read. It is not proof that any Purchase was received and must not advance a
DeliverySnapshot, mutate a Store, select an asset for a Purchase, or perform
reconciliation.

This contract performs no network call itself and has no Pipeline, Worker,
Purchase Flow, trade, confirmation, retry, sleep, or thread integration.
