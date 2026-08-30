"""Durable fail-closed guard for BUFF purchase writes.

The journal intentionally contains no Cookie, CSRF token, payment URL or other
credential material.  An intent is written before a non-idempotent request so a
process crash can only create a conservative false positive, never silently
permit another purchase write with an unknown prior outcome.
"""

from __future__ import annotations

import copy
import errno
import json
import math
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional


_GUARD_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "config"
    / "buff_checkout_guard.json"
)
_guard_lock = threading.RLock()
_activity_lock = threading.RLock()
_activity_local = threading.local()
_HISTORY_PAGE_SIZE = 10
_MAX_RECONCILIATION_PAGES = 3
_ORDER_CREATED_PENDING_STAGE = "order_created_pending"
_PAYMENT_FAILED_STATE = "FAIL"
_PAYMENT_FAILED_STATE_TEXT = "支付失败"
_REFUNDED_STATE_TEXT = "购买失败-已退款"
_REFUNDED_TIMEOUT_FIELDS = (
    "pay_expire_timeout",
    "deliver_expire_timeout",
    "receive_expire_timeout",
    "buyer_send_offer_timeout",
)
_ALLOWED_UPDATE_FIELDS = frozenset(
    {
        "stage",
        "reason",
        "order_id",
        "batch_id",
        "bill_order_ids",
        "completed_order_ids",
        "partial_results",
        "credential_generation",
        "credential_fingerprint",
        "sell_order_id",
        "quantity",
        "price",
        "last_error_type",
    }
)


class BuffCheckoutGuardActive(RuntimeError):
    """A previous checkout must be reconciled before another write."""

    def __init__(self, state: dict):
        self.state = copy.deepcopy(state)
        refs = " ".join(
            part
            for part in (
                f"order_id={state.get('order_id')}" if state.get("order_id") else "",
                f"batch_id={state.get('batch_id')}" if state.get("batch_id") else "",
                f"goods_id={state.get('goods_id')}" if state.get("goods_id") else "",
            )
            if part
        )
        suffix = f" ({refs})" if refs else ""
        super().__init__(f"存在未对账的 BUFF checkout，禁止继续购买{suffix}")


class BuffCheckoutGuardMismatch(RuntimeError):
    """A stale caller tried to mutate a different checkout intent."""


def _invalid_journal_state(stage: str, reason: str) -> dict:
    return {
        "version": 1,
        "unresolved": True,
        "stage": stage,
        "reason": reason,
        "updated_at": time.time(),
    }


def _reject_non_finite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is not allowed: {key}")
        result[key] = value
    return result


def _valid_timestamp(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _journal_is_valid(raw: dict) -> bool:
    """Only accept records written by this module.

    A syntactically valid but incomplete JSON object is just as unsafe as a
    truncated file: treating it as "no checkout" would permit another POST.
    """

    if type(raw.get("version")) is not int or raw.get("version") != 1:
        return False
    if type(raw.get("unresolved")) is not bool:
        return False
    if not isinstance(raw.get("intent_id"), str) or not raw["intent_id"].strip():
        return False
    if not isinstance(raw.get("kind"), str) or not raw["kind"].strip():
        return False
    if not isinstance(raw.get("stage"), str) or not raw["stage"].strip():
        return False
    if not _valid_timestamp(raw.get("created_at")):
        return False
    if not _valid_timestamp(raw.get("updated_at")):
        return False
    if type(raw.get("goods_id")) is not int:
        return False
    if type(raw.get("quantity")) is not int or raw["quantity"] < 1:
        return False

    if raw["unresolved"]:
        return raw["stage"] not in {"resolved", "acknowledged"}
    if raw["stage"] == "resolved":
        return _valid_timestamp(raw.get("resolved_at"))
    if raw["stage"] == "acknowledged":
        return _valid_timestamp(raw.get("acknowledged_at"))
    return False


def _activity_lock_path() -> Path:
    return _GUARD_PATH.with_name(f"{_GUARD_PATH.name}.lock")


def _lock_activity_file(stream: Any) -> None:
    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write(b"\0")
        stream.flush()
        os.fsync(stream.fileno())
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        deadline = time.monotonic() + 60.0
        while True:
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if (
                    exc.errno
                    not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
                    or time.monotonic() >= deadline
                ):
                    raise TimeoutError(
                        "等待 BUFF 跨进程活动锁超时或锁文件不可用"
                    ) from exc
                time.sleep(0.05)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)


def _unlock_activity_file(stream: Any) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def buff_activity_guard() -> Iterator[None]:
    """Serialize BUFF activity and checkout CAS across threads/processes.

    Callers that also need the authentication lock must acquire that lock
    first.  This context is re-entrant within a thread so guard mutations can
    safely use it while a checkout operation already owns the activity slot.
    """

    with _activity_lock:
        depth = int(getattr(_activity_local, "depth", 0) or 0)
        if depth == 0:
            lock_path = _activity_lock_path()
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            stream = open(lock_path, "a+b")
            try:
                _lock_activity_file(stream)
            except Exception:
                stream.close()
                raise
            _activity_local.stream = stream
        _activity_local.depth = depth + 1
        try:
            yield
        finally:
            remaining = int(getattr(_activity_local, "depth", 1)) - 1
            _activity_local.depth = remaining
            if remaining == 0:
                stream = getattr(_activity_local, "stream", None)
                try:
                    if stream is not None:
                        _unlock_activity_file(stream)
                finally:
                    if stream is not None:
                        stream.close()
                    try:
                        del _activity_local.stream
                    except AttributeError:
                        pass


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return str(value)[:500]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:2000]
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth=depth + 1) for item in value[:200]]
    if isinstance(value, dict):
        return {
            str(key)[:100]: _json_safe(item, depth=depth + 1)
            for key, item in list(value.items())[:200]
        }
    return str(value)[:500]


def _read_raw_locked() -> Optional[dict]:
    if not _GUARD_PATH.exists():
        return None
    try:
        raw = json.loads(
            _GUARD_PATH.read_text(encoding="utf-8"),
            parse_constant=_reject_non_finite_json,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, ValueError, TypeError):
        # A damaged journal must fail closed rather than disappear.
        return _invalid_journal_state(
            "journal_unreadable",
            "BUFF checkout 对账文件无法读取，需要人工检查",
        )
    if not isinstance(raw, dict) or not _journal_is_valid(raw):
        return _invalid_journal_state(
            "journal_invalid",
            "BUFF checkout 对账文件格式异常，需要人工检查",
        )
    return raw


def _atomic_write_locked(payload: dict) -> None:
    _GUARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{_GUARD_PATH.name}.",
        suffix=".tmp",
        dir=str(_GUARD_PATH.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(
                payload,
                stream,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, _GUARD_PATH)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def get_unresolved_checkout() -> Optional[dict]:
    with _guard_lock:
        state = _read_raw_locked()
        if not state or state.get("unresolved") is not True:
            return None
        return copy.deepcopy(state)


def begin_checkout(
    kind: str,
    goods_id: int,
    sell_order_id: str = "",
    batch_id: str = "",
    quantity: int = 1,
    credential_generation: int = 0,
    *,
    credential_fingerprint: str = "",
    price: Optional[float] = None,
) -> dict:
    """Durably record intent immediately before the first checkout POST."""

    with buff_activity_guard():
        with _guard_lock:
            existing = _read_raw_locked()
            if existing and existing.get("unresolved") is True:
                raise BuffCheckoutGuardActive(existing)
            now = time.time()
            state = {
                "version": 1,
                "unresolved": True,
                "intent_id": uuid.uuid4().hex,
                "kind": str(kind or "unknown")[:50],
                "stage": "intent_prepared",
                "goods_id": int(goods_id or 0),
                "sell_order_id": str(sell_order_id or "")[:200],
                "batch_id": str(batch_id or "")[:200],
                "quantity": max(1, int(quantity or 1)),
                "credential_generation": int(credential_generation or 0),
                "credential_fingerprint": str(credential_fingerprint or "")[:128],
                "created_at": now,
                "updated_at": now,
                "reason": "",
            }
            if price is not None:
                state["price"] = float(price)
            _atomic_write_locked(state)
            return copy.deepcopy(state)


def _assert_expected_intent(
    state: dict,
    expected_intent_id: Optional[str],
    *,
    required: bool = False,
) -> None:
    expected = str(expected_intent_id or "").strip()
    actual = str(state.get("intent_id") or "").strip()
    if required and not expected:
        raise BuffCheckoutGuardMismatch("缺少待对账 checkout 的 intent_id")
    if expected and expected != actual:
        raise BuffCheckoutGuardMismatch(
            "checkout intent_id 已变化，拒绝使用陈旧确认修改当前门禁"
        )


def update_checkout(
    *,
    expected_intent_id: Optional[str] = None,
    **fields: Any,
) -> Optional[dict]:
    """Update safe reconciliation metadata for the unresolved intent."""

    with buff_activity_guard():
        with _guard_lock:
            state = _read_raw_locked()
            if not state or state.get("unresolved") is not True:
                return None
            _assert_expected_intent(state, expected_intent_id)
            for key, value in fields.items():
                if key in _ALLOWED_UPDATE_FIELDS:
                    state[key] = _json_safe(value)
            state["updated_at"] = time.time()
            _atomic_write_locked(state)
            return copy.deepcopy(state)


def resolve_checkout(
    reason: str = "completed",
    *,
    expected_intent_id: Optional[str] = None,
) -> Optional[dict]:
    """Mark the intent resolved after a durable purchase record or known reject."""

    with buff_activity_guard():
        with _guard_lock:
            state = _read_raw_locked()
            if not state:
                return None
            _assert_expected_intent(state, expected_intent_id)
            now = time.time()
            state.update(
                {
                    "unresolved": False,
                    "stage": "resolved",
                    "reason": str(reason or "completed")[:2000],
                    "resolved_at": now,
                    "updated_at": now,
                }
            )
            _atomic_write_locked(state)
            return copy.deepcopy(state)


def _parse_history_page(
    payload: Any,
    expected_page_num: int,
) -> tuple[int, list[dict]] | None:
    """Parse only the bounded, identity-bearing part of one history page."""

    if not isinstance(payload, dict) or payload.get("code") != "OK":
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    page_num = data.get("page_num")
    page_size = data.get("page_size")
    total_page = data.get("total_page")
    items = data.get("items")
    if (
        type(page_num) is not int
        or page_num != expected_page_num
        or type(page_size) is not int
        or page_size != _HISTORY_PAGE_SIZE
        or type(total_page) is not int
        or total_page < page_num
        or not isinstance(items, list)
        or len(items) > _HISTORY_PAGE_SIZE
        or any(
            not isinstance(item, dict)
            or type(item.get("id")) is not str
            or not item["id"]
            or item["id"].strip() != item["id"]
            for item in items
        )
    ):
        return None
    return total_page, items


def _history_item_terminal_reason(item: dict) -> str | None:
    """Return the only history terminal states safe for guard resolution."""

    if (
        item.get("state") == _PAYMENT_FAILED_STATE
        and item.get("state_text") == _PAYMENT_FAILED_STATE_TEXT
    ):
        return "auto_reconciled_payment_failed"
    if (
        item.get("state") != _PAYMENT_FAILED_STATE
        or item.get("state_text") != _REFUNDED_STATE_TEXT
        or any(
            field not in item
            or type(item[field]) not in (int, float)
            or isinstance(item[field], bool)
            or not math.isfinite(item[field])
            or item[field] != -1
            for field in _REFUNDED_TIMEOUT_FIELDS
        )
        or "tradeofferid" not in item
        or item["tradeofferid"] is not None
        or "trade_offer_url" not in item
        or item["trade_offer_url"] is not None
    ):
        return None
    return "auto_reconciled_refunded"


def reconcile_order_created_pending(buff_client: object) -> bool:
    """Resolve one exact unpaid checkout from bounded BUFF history evidence.

    This is intentionally a one-shot read helper.  It never creates a client,
    retries a request, mutates purchases, or calls a BUFF write endpoint.
    """

    reader = getattr(buff_client, "get_buy_order_history_page", None)
    if not callable(reader):
        return False

    with buff_activity_guard():
        state = get_unresolved_checkout()
        if not isinstance(state, dict):
            return False
        if state.get("stage") != _ORDER_CREATED_PENDING_STAGE:
            return False
        order_id = state.get("order_id")
        intent_id = state.get("intent_id")
        if (
            type(order_id) is not str
            or not order_id
            or order_id.strip() != order_id
            or type(intent_id) is not str
            or not intent_id
        ):
            return False

        for page_num in range(1, _MAX_RECONCILIATION_PAGES + 1):
            try:
                payload = reader(page_num, "csgo")
            except Exception:
                return False
            parsed = _parse_history_page(payload, page_num)
            if parsed is None:
                return False
            total_page, items = parsed
            matches = [item for item in items if item["id"] == order_id]
            if len(matches) > 1:
                return False
            if len(matches) == 1:
                item = matches[0]
                terminal_reason = _history_item_terminal_reason(item)
                if terminal_reason is None:
                    return False
                current = get_unresolved_checkout()
                if (
                    not isinstance(current, dict)
                    or current.get("intent_id") != intent_id
                    or current.get("stage") != _ORDER_CREATED_PENDING_STAGE
                    or current.get("order_id") != order_id
                ):
                    return False
                try:
                    resolved = resolve_checkout(
                        terminal_reason,
                        expected_intent_id=intent_id,
                    )
                except Exception:
                    return False
                return bool(
                    isinstance(resolved, dict)
                    and resolved.get("unresolved") is False
                    and resolved.get("stage") == "resolved"
                )
            if page_num >= total_page:
                break
        return False


def acknowledge_checkout(
    expected_intent_id: str,
    reason: str = "user_reconciled",
) -> Optional[dict]:
    """Explicit user acknowledgement after checking the BUFF order history."""

    with buff_activity_guard():
        with _guard_lock:
            state = _read_raw_locked()
            if not state or state.get("unresolved") is not True:
                return None
            _assert_expected_intent(
                state,
                expected_intent_id,
                required=True,
            )
            now = time.time()
            state.update(
                {
                    "unresolved": False,
                    "stage": "acknowledged",
                    "reason": str(reason or "user_reconciled")[:2000],
                    "acknowledged_at": now,
                    "updated_at": now,
                }
            )
            _atomic_write_locked(state)
            return copy.deepcopy(state)


__all__ = [
    "BuffCheckoutGuardActive",
    "BuffCheckoutGuardMismatch",
    "acknowledge_checkout",
    "begin_checkout",
    "buff_activity_guard",
    "get_unresolved_checkout",
    "reconcile_order_created_pending",
    "resolve_checkout",
    "update_checkout",
]
