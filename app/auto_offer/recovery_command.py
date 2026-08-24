"""One-shot OWNER command for exact recovery-only Auto Offer maintenance.

This module deliberately does not import the FastAPI app, task queue, workers,
pipeline, or normal Auto Offer runtime. ``preflight`` is local-only and
zero-network. ``execute`` first repeats the same local proof, then constructs
only the reviewed recovery-only Host facade.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Sequence

from app.accounts import get_account, get_current_id
from app.auto_offer.contracts import (
    AutoOfferResult,
    DeliveryMode,
    DeliveryStatus,
    TERMINAL_DELIVERY_STATUSES,
)
from app.auto_offer.preflight_snapshot import collect_local_preflight_snapshot
from app.auto_offer.store import AutoOfferStore, StoredDelivery
from app.database import db_complete_purchase_receipt_by_id, db_get_purchases
from app.services.buff_client import BuffClient
from config import get as get_credential_value


_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_HOST_DB_PATH: Final[Path] = _ROOT / "config" / "app.db"
_STORE_PATH: Final[Path] = _ROOT / "config" / "auto_offer.db"
_MAX_TICKS: Final[int] = 6
_FINGERPRINT_SCHEMA: Final[str] = "task049-target-v2"

_ALLOWED_TRANSITIONS: Final[dict[DeliveryStatus, frozenset[DeliveryStatus]]] = {
    DeliveryStatus.RESULT_UNKNOWN: frozenset({DeliveryStatus.OFFER_SENT}),
    DeliveryStatus.OFFER_SENT: frozenset(
        {
            DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
            DeliveryStatus.OFFER_CONFIRMED,
        }
    ),
    DeliveryStatus.OFFER_CONFIRMED: frozenset(
        {DeliveryStatus.AWAITING_INVENTORY}
    ),
    DeliveryStatus.AWAITING_INVENTORY: frozenset({DeliveryStatus.RECEIVED}),
}


class RecoveryCommandError(RuntimeError):
    """Fail-closed command error carrying only a fixed reason code."""


@dataclass(frozen=True, slots=True)
class RecoveryTargetBinding:
    source_commit: str
    source_tree: str
    fingerprint: str
    order_id: str
    host_db_id: int
    store: StoredDelivery
    account_id: str
    recipient_steam_id: str
    steam_cookie: str
    buff_cookie: str
    buff_user_agent: str | None
    buff_generation: int


def _fail(reason: str) -> RecoveryCommandError:
    return RecoveryCommandError(reason)


def _strict_hex(value: object, length: int, reason: str) -> str:
    if (
        type(value) is not str
        or len(value) != length
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise _fail(reason)
    return value


def _strict_text(value: object, reason: str) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or any(ord(ch) < 32 for ch in value)
    ):
        raise _fail(reason)
    return value


def _canonical_steam_id(value: object) -> str:
    text = _strict_text(value, "recipient_identity_invalid")
    if (
        not text.isascii()
        or not text.isdecimal()
        or text[0] == "0"
        or int(text) <= 0
        or str(int(text)) != text
    ):
        raise _fail("recipient_identity_invalid")
    return text


def _git(*args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except Exception:
        raise _fail("source_provenance_unavailable") from None
    if completed.returncode != 0:
        raise _fail("source_provenance_unavailable")
    return completed.stdout.strip()


def _verify_source(expected_commit: str, expected_tree: str) -> tuple[str, str]:
    expected_commit = _strict_hex(
        expected_commit, 40, "expected_commit_invalid"
    )
    expected_tree = _strict_hex(expected_tree, 40, "expected_tree_invalid")
    head = _strict_hex(_git("rev-parse", "HEAD"), 40, "source_head_invalid")
    tree = _strict_hex(
        _git("rev-parse", "HEAD^{tree}"), 40, "source_tree_invalid"
    )
    if head != expected_commit or tree != expected_tree:
        raise _fail("source_provenance_mismatch")
    if _git("status", "--porcelain", "--untracked-files=no"):
        raise _fail("tracked_source_not_clean")
    return head, tree


def _credential_snapshot(
    *,
    store_account_id: str,
    store_recipient_steam_id: str,
) -> tuple[str, str, str | None, int]:
    current_id = _strict_text(get_current_id(), "current_account_invalid")
    account = get_account(current_id)
    if not isinstance(account, dict) or account.get("id") != current_id:
        raise _fail("current_account_invalid")
    account_steam_id = _canonical_steam_id(account.get("steam_id"))
    if current_id != store_account_id or account_steam_id != store_recipient_steam_id:
        raise _fail("account_store_identity_mismatch")

    steam_id = _canonical_steam_id(
        get_credential_value("steam", "steam_id", "")
    )
    steam_cookie = get_credential_value("steam", "cookies", "")
    if steam_id != store_recipient_steam_id:
        raise _fail("steam_credential_identity_mismatch")
    if type(steam_cookie) is not str or not steam_cookie:
        raise _fail("steam_cookie_missing")

    buff_cookie = get_credential_value("buff", "cookies", "")
    if type(buff_cookie) is not str or not buff_cookie:
        raise _fail("buff_cookie_missing")
    user_agent = get_credential_value("buff", "user_agent", None)
    if user_agent is not None:
        if type(user_agent) is not str or not user_agent.strip():
            raise _fail("buff_user_agent_invalid")
        user_agent = user_agent.strip()
    generation_raw = get_credential_value("buff", "generation", 0)
    if type(generation_raw) is bool:
        raise _fail("buff_generation_invalid")
    try:
        generation = int(generation_raw or 0)
    except (TypeError, ValueError):
        raise _fail("buff_generation_invalid") from None
    if generation < 0:
        raise _fail("buff_generation_invalid")
    return steam_cookie, buff_cookie, user_agent, generation


def _validate_initial_store_target(stored: StoredDelivery) -> None:
    snapshot = stored.snapshot
    if (
        snapshot.delivery_mode is not DeliveryMode.BUYER_SENDS_OFFER
        or snapshot.delivery_status is not DeliveryStatus.RESULT_UNKNOWN
        or snapshot.delivery_error != "write_result_unknown"
        or snapshot.offer_attempted_at is None
        or snapshot.offer_sent_at is not None
        or snapshot.received_at is not None
        or snapshot.steam_tradeoffer_id is not None
        or snapshot.counterparty_steam_id is not None
        or snapshot.pending_receipt is not True
        or snapshot.assetid is not None
        or type(stored.revision) is not int
        or stored.revision < 1
    ):
        raise _fail("target_store_state_not_recoverable")


def _credential_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _target_fingerprint(
    *,
    source_commit: str,
    source_tree: str,
    host_db_id: int,
    host_order_id: str,
    stored: StoredDelivery,
    steam_cookie: str,
    buff_cookie: str,
    buff_user_agent: str | None,
    buff_generation: int,
) -> str:
    snapshot = stored.snapshot
    ordered = [
        ["schema", _FINGERPRINT_SCHEMA],
        ["source_commit", source_commit],
        ["source_tree", source_tree],
        ["host_db_id", host_db_id],
        ["host_buff_order_id", host_order_id],
        ["store_purchase_id", snapshot.purchase_id],
        ["store_buff_order_id", snapshot.buff_order_id],
        ["store_account_id", snapshot.account_id],
        ["store_recipient_steam_id", snapshot.recipient_steam_id],
        ["store_revision", stored.revision],
        ["store_delivery_mode", snapshot.delivery_mode.value],
        ["store_delivery_status", snapshot.delivery_status.value],
        ["store_delivery_error", snapshot.delivery_error],
        ["store_offer_attempted_at", snapshot.offer_attempted_at],
        ["store_offer_sent_at", snapshot.offer_sent_at],
        ["store_received_at", snapshot.received_at],
        ["store_tradeoffer_id", snapshot.steam_tradeoffer_id],
        ["store_counterparty_steam_id", snapshot.counterparty_steam_id],
        ["store_pending_receipt", snapshot.pending_receipt],
        ["store_assetid", snapshot.assetid],
        ["steam_cookie_sha256", _credential_digest(steam_cookie)],
        [
            "buff_credential_sha256",
            _credential_digest(
                f"{buff_cookie}\0{buff_user_agent or ''}\0{buff_generation}"
            ),
        ],
    ]
    payload = json.dumps(
        ordered,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _assert_exact_host_order_readonly(
    host_db_path: Path,
    *,
    order_id: str,
    expected_db_id: int,
    expected_pending: bool,
    expected_assetid: str | None,
) -> None:
    """Prove exact Host order multiplicity without creating or writing SQLite."""

    try:
        path = Path(host_db_path).expanduser().resolve(strict=True)
        if path.is_symlink() or not path.is_file():
            raise _fail("host_db_source_invalid")
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro&immutable=1",
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
    except RecoveryCommandError:
        raise
    except (OSError, sqlite3.DatabaseError, RuntimeError, ValueError):
        raise _fail("host_db_readonly_open_failed") from None
    try:
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute(
            "SELECT id, pending_receipt, assetid FROM purchase "
            "WHERE buff_order_id = ? ORDER BY id ASC",
            (order_id,),
        ).fetchall()
    except sqlite3.DatabaseError:
        raise _fail("host_exact_order_read_failed") from None
    finally:
        try:
            connection.close()
        except sqlite3.DatabaseError:
            raise _fail("host_db_readonly_close_failed") from None

    expected_pending_int = 1 if expected_pending else 0
    if (
        len(rows) != 1
        or len(rows[0]) != 3
        or type(rows[0][0]) is not int
        or rows[0][0] != expected_db_id
        or type(rows[0][1]) is not int
        or rows[0][1] != expected_pending_int
        or rows[0][2] != expected_assetid
    ):
        raise _fail("host_exact_order_identity_changed")


def collect_recovery_preflight(
    *,
    expected_commit: str,
    expected_tree: str,
    host_db_path: Path = _HOST_DB_PATH,
    store_path: Path = _STORE_PATH,
) -> RecoveryTargetBinding:
    source_commit, source_tree = _verify_source(expected_commit, expected_tree)
    local = collect_local_preflight_snapshot(
        host_db_path=host_db_path,
        auto_offer_store_path=store_path,
    )
    if len(local.host_pending) != 1:
        raise _fail("host_target_not_exclusive")
    host = local.host_pending[0]
    if host.assetid is not None:
        raise _fail("host_target_asset_already_bound")
    _assert_exact_host_order_readonly(
        host_db_path,
        order_id=host.buff_order_id,
        expected_db_id=host.host_db_id,
        expected_pending=True,
        expected_assetid=None,
    )

    rows = AutoOfferStore.inspect_existing(store_path)
    matches = [row for row in rows if row.snapshot.buff_order_id == host.buff_order_id]
    if len(matches) != 1:
        raise _fail("store_target_not_exclusive")
    stored = matches[0]
    unrelated_recoverable = [
        row
        for row in rows
        if row.snapshot.buff_order_id != host.buff_order_id
        and row.snapshot.delivery_status not in TERMINAL_DELIVERY_STATUSES
    ]
    if unrelated_recoverable:
        raise _fail("unrelated_recoverable_store_row")
    if (
        stored.snapshot.purchase_id != f"buff:{host.buff_order_id}"
        or stored.snapshot.buff_order_id != host.buff_order_id
    ):
        raise _fail("host_store_identity_mismatch")
    _validate_initial_store_target(stored)

    account_id = _strict_text(stored.snapshot.account_id, "store_account_invalid")
    recipient = _canonical_steam_id(stored.snapshot.recipient_steam_id)
    steam_cookie, buff_cookie, buff_user_agent, buff_generation = _credential_snapshot(
        store_account_id=account_id,
        store_recipient_steam_id=recipient,
    )
    fingerprint = _target_fingerprint(
        source_commit=source_commit,
        source_tree=source_tree,
        host_db_id=host.host_db_id,
        host_order_id=host.buff_order_id,
        stored=stored,
        steam_cookie=steam_cookie,
        buff_cookie=buff_cookie,
        buff_user_agent=buff_user_agent,
        buff_generation=buff_generation,
    )
    return RecoveryTargetBinding(
        source_commit=source_commit,
        source_tree=source_tree,
        fingerprint=fingerprint,
        order_id=host.buff_order_id,
        host_db_id=host.host_db_id,
        store=stored,
        account_id=account_id,
        recipient_steam_id=recipient,
        steam_cookie=steam_cookie,
        buff_cookie=buff_cookie,
        buff_user_agent=buff_user_agent,
        buff_generation=buff_generation,
    )


def _assert_store_preexecution_stable(binding: RecoveryTargetBinding) -> None:
    """Repeat detached Store admission before any RW Store handle is opened."""

    rows = AutoOfferStore.inspect_existing(_STORE_PATH)
    matches = [row for row in rows if row.snapshot.buff_order_id == binding.order_id]
    if len(matches) != 1 or matches[0] != binding.store:
        raise _fail("store_target_changed_before_execution")
    if any(
        row.snapshot.buff_order_id != binding.order_id
        and row.snapshot.delivery_status not in TERMINAL_DELIVERY_STATUSES
        for row in rows
    ):
        raise _fail("unrelated_recoverable_store_row")


def _read_store_target(order_id: str, store_path: Path = _STORE_PATH) -> StoredDelivery:
    stored = AutoOfferStore.inspect_existing_by_buff_order_id(store_path, order_id)
    if stored is None:
        raise _fail("store_target_disappeared")
    return stored


def _host_rows() -> list:
    return db_get_purchases()


def _make_buff_client(binding: RecoveryTargetBinding) -> BuffClient:
    # Intentionally no credentials_provider and no credentials_update_callback:
    # read-side BUFF CookieJar changes cannot persist credentials during this
    # maintenance command.
    return BuffClient(
        binding.buff_cookie,
        user_agent=binding.buff_user_agent,
        credential_generation=binding.buff_generation,
        credentials_provider=None,
        credentials_update_callback=None,
    )


def _make_maintenance(
    buff_client: BuffClient,
    binding: RecoveryTargetBinding,
):
    # Do not call build_host_recovery_only_maintenance(): its convenience
    # credential lookup currently routes through load_app_config()/get_steam(),
    # which can migrate legacy config and validates identity_secret.  TASK-049
    # already owns an exact fingerprint-bound cookie snapshot, so inject it
    # directly into the same TASK-048 recovery-only bridge instead.
    from app.auto_offer.host_integration import (
        HostRecoveryOnlyMaintenance,
        _build_recovery_only_host_auto_offer_bridge,
    )

    bridge = _build_recovery_only_host_auto_offer_bridge(
        buff_client=buff_client,
        account_id=binding.account_id,
        account_steam_id=binding.recipient_steam_id,
        steam_cookie_string=binding.steam_cookie,
        store_path=_STORE_PATH,
    )
    return HostRecoveryOnlyMaintenance(
        bridge,
        complete_purchase_receipt_by_id=db_complete_purchase_receipt_by_id,
    )


def _same_identity(before: StoredDelivery, after: StoredDelivery) -> bool:
    return (
        after.snapshot.purchase_id == before.snapshot.purchase_id
        and after.snapshot.buff_order_id == before.snapshot.buff_order_id
        and after.snapshot.account_id == before.snapshot.account_id
        and after.snapshot.recipient_steam_id == before.snapshot.recipient_steam_id
    )


def _valid_one_step(before: StoredDelivery, after: StoredDelivery) -> bool:
    if not _same_identity(before, after):
        return False
    if after.revision != before.revision + 1 or after.snapshot == before.snapshot:
        return False
    return after.snapshot.delivery_status in _ALLOWED_TRANSITIONS.get(
        before.snapshot.delivery_status, frozenset()
    )


def _assert_binding_stable(binding: RecoveryTargetBinding) -> None:
    """Recheck local authority immediately before each possible platform read."""

    _verify_source(binding.source_commit, binding.source_tree)
    _assert_exact_host_order_readonly(
        _HOST_DB_PATH,
        order_id=binding.order_id,
        expected_db_id=binding.host_db_id,
        expected_pending=True,
        expected_assetid=None,
    )
    credentials = _credential_snapshot(
        store_account_id=binding.account_id,
        store_recipient_steam_id=binding.recipient_steam_id,
    )
    if credentials != (
        binding.steam_cookie,
        binding.buff_cookie,
        binding.buff_user_agent,
        binding.buff_generation,
    ):
        raise _fail("credential_snapshot_changed")


def _verify_host_receipt(binding: RecoveryTargetBinding, assetid: str) -> bool:
    rows = [
        row
        for row in _host_rows()
        if row.get("buff_order_id") == binding.order_id
    ]
    return (
        len(rows) == 1
        and rows[0].get("_db_id") == binding.host_db_id
        and rows[0].get("pending_receipt") is False
        and rows[0].get("assetid") == assetid
    )


def execute_recovery(
    binding: RecoveryTargetBinding,
    *,
    expected_fingerprint: str,
) -> int:
    expected_fingerprint = _strict_hex(
        expected_fingerprint, 64, "expected_fingerprint_invalid"
    )
    if binding.fingerprint != expected_fingerprint:
        raise _fail("target_fingerprint_mismatch")

    _assert_binding_stable(binding)
    _assert_store_preexecution_stable(binding)
    buff_client = _make_buff_client(binding)
    maintenance = None
    try:
        maintenance = _make_maintenance(buff_client, binding)
        for tick in range(1, _MAX_TICKS + 1):
            _assert_binding_stable(binding)
            before = _read_store_target(binding.order_id)
            if before.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMATION_REQUIRED:
                print(
                    "TASK049_RECOVERY_STOPPED "
                    f"reason=confirmation_required tick={tick} "
                    f"revision={before.revision} status={before.snapshot.delivery_status.value}"
                )
                return 2
            outcome = maintenance.run_recovery_tick(_host_rows())
            after = _read_store_target(binding.order_id)

            persisted = _valid_one_step(before, after)
            print(
                "TASK049_RECOVERY_TICK "
                f"tick={tick} before_revision={before.revision} "
                f"before_status={before.snapshot.delivery_status.value} "
                f"after_revision={after.revision} "
                f"after_status={after.snapshot.delivery_status.value} "
                f"result={outcome.result.value} persisted={str(persisted).lower()}"
            )

            if outcome.result in {AutoOfferResult.BLOCKED, AutoOfferResult.RESULT_UNKNOWN}:
                print(
                    "TASK049_RECOVERY_STOPPED "
                    f"reason={outcome.result.value} tick={tick}"
                )
                return 2
            if not persisted:
                print(
                    "TASK049_RECOVERY_STOPPED "
                    f"reason=no_single_persisted_transition tick={tick}"
                )
                return 2
            if after.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMATION_REQUIRED:
                print(
                    "TASK049_RECOVERY_STOPPED "
                    f"reason=confirmation_required tick={tick} "
                    f"revision={after.revision}"
                )
                return 2
            if after.snapshot.delivery_status is DeliveryStatus.RECEIVED:
                assetid = after.snapshot.assetid
                if type(assetid) is not str or not assetid or after.snapshot.pending_receipt is not False:
                    raise _fail("received_store_evidence_invalid")
                if outcome.result is not AutoOfferResult.COMPLETE:
                    raise _fail("received_outcome_invalid")
                _assert_binding_stable(binding)
                if maintenance.complete_host_receipt(_host_rows()) is not True:
                    raise _fail("host_receipt_not_completed")
                if not _verify_host_receipt(binding, assetid):
                    raise _fail("host_receipt_verification_failed")
                print(
                    "TASK049_RECOVERY_COMPLETE "
                    f"ticks={tick} store_revision={after.revision} "
                    "store_status=received host_pending=false host_asset_bound=true"
                )
                return 0
            if outcome.result is not AutoOfferResult.WAITING:
                raise _fail("unexpected_recovery_outcome")

        print(
            "TASK049_RECOVERY_STOPPED "
            f"reason=tick_limit max_ticks={_MAX_TICKS}"
        )
        return 2
    finally:
        if maintenance is not None:
            try:
                maintenance.close()
            except Exception:
                pass
        try:
            buff_client.close()
        except Exception:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.auto_offer.recovery_command",
        description="Exact one-shot recovery-only maintenance for TASK-049.",
    )
    sub = parser.add_subparsers(dest="mode", required=True)
    for name in ("preflight", "execute"):
        child = sub.add_parser(name)
        child.add_argument("--expected-commit", required=True)
        child.add_argument("--expected-tree", required=True)
        if name == "execute":
            child.add_argument("--expected-fingerprint", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        binding = collect_recovery_preflight(
            expected_commit=args.expected_commit,
            expected_tree=args.expected_tree,
        )
        if args.mode == "preflight":
            print(
                "TASK049_RECOVERY_PREFLIGHT_READY "
                f"fingerprint={binding.fingerprint} "
                f"store_revision={binding.store.revision} "
                f"store_status={binding.store.snapshot.delivery_status.value} "
                "host_pending=true host_asset_bound=false "
                "unrelated_recoverable=0 account_identity_match=true "
                "steam_cookie_present=true buff_cookie_present=true network_requests=0 mutations=0"
            )
            return 0
        return execute_recovery(
            binding,
            expected_fingerprint=args.expected_fingerprint,
        )
    except RecoveryCommandError as exc:
        print(f"TASK049_RECOVERY_BLOCKED reason={exc}")
        return 2
    except Exception:
        print("TASK049_RECOVERY_BLOCKED reason=unexpected_local_or_execution_error")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
