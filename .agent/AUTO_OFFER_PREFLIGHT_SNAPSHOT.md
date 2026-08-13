# Auto Offer Phase-A Local Snapshot Contract — TASK-038

TASK-038 exists only to remove TASK-037 BLOCKER-01. It provides a detached local-state collector for a future credentialed canary preflight. It does **not** perform that preflight, access a real account, arm a canary, or authorize any live write.

**REAL-WRITE GATE remains CLOSED.**

## Why the historical “read-only” runtime is not used

`HostReadOnlyAutoOfferBridge` and `ReadOnlyAutoOfferRuntime` are platform-write-safe historical helpers, not zero-local-write preflight primitives.

They may construct and initialize `AutoOfferStore`, and reviewed read progression can persist a Store transition. That behavior is correct for their historical purpose but incompatible with TASK-037 Phase A, which requires no Host/Store mutation.

TASK-038 therefore does not modify, wrap, rename, or reuse those helpers.

## Source isolation model

`collect_local_preflight_snapshot(...)` never gives SQLite a production database path.

For each source it:

1. resolves the caller-supplied Host DB and Auto Offer Store paths without creating them;
2. fingerprints the main file and SQLite sidecar family (`-wal`, `-shm`, `-journal`);
3. requires the source to be quiescent before collection:
   - Host DB must exist as a regular file;
   - Auto Offer Store may be absent;
   - an existing source must have no WAL/SHM/journal sidecar;
4. reads only the main database file bytes through ordinary file-read I/O;
5. validates the SQLite header;
6. if a cleanly closed database retains WAL header bytes, normalizes only bytes 18/19 of the detached copy from WAL mode to rollback mode; the source file is never changed;
7. deserializes the detached image into `sqlite3.connect(":memory:")`;
8. enables connection-local `PRAGMA query_only = ON` and reads only that in-memory image;
9. validates the required schema and row contracts;
10. fingerprints both source families again and rejects the entire result if any file identity/content/mtime/size changed during collection.

This avoids SQLite VFS access to the source files entirely. In particular, SQLite cannot create or update a source `-shm`, WAL, journal, schema, `user_version`, or journal-mode setting.

If a WAL/SHM/journal exists, the collector does not attempt recovery, checkpointing, copying through the SQLite backup API, or a stale “immutable” read. It fails closed with `sqlite_source_not_quiescent`.

## Host evidence

Only rows with `purchase.pending_receipt = 1` are returned.

Each `HostPendingPurchaseSnapshot` contains only:

- Host primary key (`host_db_id`);
- exact BUFF order ID;
- existing asset ID, if any.

The collector does not silently choose a target. If two pending purchases exist, both remain in the detached evidence so the later Phase-A gate can reject the state.

Duplicate BUFF order identities or malformed pending rows fail closed.

The collector imports no Host SQLAlchemy engine/session and exposes no Host mutation function.

## Auto Offer Store evidence

An absent Store is represented as `store_exists=False` and is not created.

An existing Store must match exact Store schema version 1, including:

- exact table set;
- exact column definitions;
- AUTOINCREMENT identity;
- unique `purchase_id`;
- unique `buff_order_id`.

All persisted delivery rows are validated through the existing `DeliverySnapshot` contract before detached evidence is returned.

Each `AutoOfferDeliverySnapshot` contains only:

- purchase ID;
- BUFF order ID;
- Auto Offer account ID;
- recipient SteamID;
- delivery mode/status;
- exact bound Trade Offer ID, if any;
- pending-receipt flag;
- asset ID, if any;
- revision.

No Store object or connection escapes the collector.

## No capability surfaces

The module does not import or expose:

- `AutoOfferStore.initialize`, registration, advance, or CAS;
- `DeliveryCoordinator`;
- Host writeback/database mutation APIs;
- canary authority arm/rearm/completion;
- platform adapters/transports;
- requests/session/network clients;
- pipeline attachment;
- worker, poller, scheduler, retry, resend, or reconfirm logic.

Returned dataclasses are frozen and detached.

## Secret boundary

The collector does not read or return credentials.

Evidence intentionally omits:

- Steam cookies / `steamLoginSecure`;
- session IDs;
- identity/shared secrets;
- access/refresh/authorization tokens;
- confirmation signatures/nonces/timestamps;
- raw `buyer_info`;
- HTTP bodies/headers;
- credential-bearing URLs;
- unrelated Host text fields such as item names.

Error messages are fixed reason codes and do not interpolate source paths, row contents, or exception strings.

## Concurrency rule

The collector is not a lock and does not claim to establish external custody.

Instead, source-family SHA-256, device/inode, size, and mtime fingerprints must remain exactly unchanged across the complete Host+Store collection. Any observed source change fails the collection.

The later TASK-037 operating gate still must separately prove no current-version writer, old binary, external host, manual actor, bot/script, or direct DB writer can race the real canary.

## Verification boundary

TASK-038 verification is fake/local-only.

Required proofs include:

- absent sources are never created;
- clean existing sources are byte/mtime stable;
- SQLite only opens `:memory:` images;
- cleanly closed WAL-header databases can be inspected from detached copies without source mutation;
- any WAL/SHM/journal sidecar fails closed;
- Store mutator methods can be monkeypatched to explode without affecting the collector;
- two Host pending rows are preserved;
- malformed/duplicate/schema-invalid state fails closed;
- concurrent source change is detected;
- returned evidence is immutable and secret-safe;
- no source sidecar or network I/O is created.

TASK-038 merge, if later accepted, removes only TASK-037 BLOCKER-01. It does not authorize TASK-037 Phase A.

**REAL-WRITE GATE remains CLOSED.**
