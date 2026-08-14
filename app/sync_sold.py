import time
from typing import Any, Dict, List, Tuple


_EXPECTED_PURCHASE_FIELDS = (
    "name",
    "buff_order_id",
    "assetid",
    "pending_receipt",
    "sale_price",
    "sold_at",
    "listing",
    "listing_status",
)


def _plan_assetid_fills(
    purchases: List[dict],
    inv_items: List[dict],
    eligible_indexes: set[int],
) -> dict[int, dict[str, object]]:
    used_assetids = {str(p.get("assetid")) for p in purchases if p.get("assetid")}
    proposals: dict[int, dict[str, object]] = {}
    for index, purchase in enumerate(purchases):
        if index not in eligible_indexes or purchase.get("assetid"):
            continue
        pname = (purchase.get("name") or "").strip()
        if not pname:
            continue
        for item in inv_items:
            assetid = str(item.get("assetid") or "")
            if not assetid or assetid in used_assetids:
                continue
            market_hash_name = (item.get("market_hash_name") or "").strip()
            inventory_name = (item.get("name") or "").strip()
            if market_hash_name == pname or inventory_name == pname:
                proposals[index] = {
                    "assetid": assetid,
                    "listing": False,
                    "listing_status": None,
                    "pending_receipt": False,
                }
                used_assetids.add(assetid)
                break
    return proposals


def _expected_purchase_snapshot(purchase: dict) -> dict[str, object]:
    return {
        field: purchase.get(field)
        for field in _EXPECTED_PURCHASE_FIELDS
    }


def run_sync_sold_from_history(log_fn=None) -> Tuple[bool, Dict[str, Any]]:
    from app.config_loader import get_steam_credentials
    from app.state import get_state
    from app.auto_offer.host_ownership import (
        HostPurchaseMutationBlockedError,
        HostPurchaseOwnership,
        classify_host_purchases,
    )
    cred = get_steam_credentials()
    cookies = cred.get("cookies") or ""
    if not cookies:
        return False, {"error": "未配置 Steam Cookie"}
    _state = get_state()
    purchases = _state.get_purchases()
    sales = _state.get_sales()
    try:
        ownership = classify_host_purchases(purchases)
    except HostPurchaseMutationBlockedError as exc:
        return False, {"error": exc.code}
    if any(item.ownership is HostPurchaseOwnership.UNSAFE for item in ownership):
        return False, {"error": "AUTO_OFFER_OWNERSHIP_UNSAFE"}

    fill_proposals: dict[int, dict[str, object]] = {}
    try:
        from app.inventory_cs2 import scan_cs2_inventory
        if log_fn:
            log_fn("正在拉取 CS2 库存…", "info")
        ok, inv_items, err = scan_cs2_inventory()
        if ok and inv_items:
            eligible_indexes = {
                index
                for index, decision in enumerate(ownership)
                if decision.ownership is HostPurchaseOwnership.UNOWNED
            }
            fill_proposals = _plan_assetid_fills(
                purchases,
                inv_items,
                eligible_indexes,
            )
            if log_fn and fill_proposals:
                log_fn(f"库存匹配填充 assetid {len(fill_proposals)} 条", "info")
        elif not ok and log_fn:
            log_fn(f"拉取库存失败: {err}", "warn")
    except Exception as e:
        if log_fn:
            log_fn(f"拉取/匹配库存异常: {e}", "warn")
    from app.steam_listings import fetch_my_history_sold
    c = cookies if isinstance(cookies, dict) else {}
    if not isinstance(cookies, dict):
        for part in (cookies or "").split(";"):
            s = part.strip()
            if "=" in s:
                k, _, v = s.partition("=")
                c[k.strip()] = v.strip()
    if not c.get("steamLoginSecure"):
        return False, {"error": "Cookie 中无 steamLoginSecure，请重新登录 Steam"}
    if log_fn:
        log_fn("正在拉取 Steam 市场历史 Sold 记录…", "info")
    ok, sold_map, err_msg = fetch_my_history_sold(c, debug_fn=None)
    if not ok:
        return False, {"error": err_msg or "拉取市场历史失败"}
    if log_fn:
        log_fn(f"解析到售出 {len(sold_map)} 条", "info")
    updates: dict[int, dict[str, object]] = {
        index: dict(proposal)
        for index, proposal in fill_proposals.items()
    }
    sold_at = time.time()
    for i, p in enumerate(purchases):
        if ownership[i].ownership not in {
            HostPurchaseOwnership.UNOWNED,
            HostPurchaseOwnership.RELEASED,
        }:
            continue
        candidate = dict(p)
        candidate.update(fill_proposals.get(i, {}))
        aid = str(candidate.get("assetid") or "").strip()
        if not aid or aid not in sold_map:
            continue
        if candidate.get("sale_price") is not None and float(candidate.get("sale_price") or 0) > 0:
            continue
        updates.setdefault(i, {}).update(
            {
                "sale_price": sold_map[aid],
                "sold_at": sold_at,
                "listing": False,
                "listing_status": None,
            }
        )

    updated = 0
    applied_fills = 0
    for index, data in updates.items():
        db_id = purchases[index].get("_db_id")
        if type(db_id) is not int or db_id <= 0:
            continue
        try:
            applied = _state.update_purchase_by_id_if_matches(
                db_id,
                data,
                _expected_purchase_snapshot(purchases[index]),
            )
        except HostPurchaseMutationBlockedError as exc:
            if exc.code == "AUTO_OFFER_PURCHASE_MANAGED":
                continue
            return False, {"error": exc.code, "updated": updated, "filled": applied_fills, "sold_count": len(sold_map)}
        if not applied:
            continue
        if index in fill_proposals:
            applied_fills += 1
        if any(field in data for field in ("sale_price", "sold_at")):
            updated += 1
    return True, {"updated": updated, "filled": applied_fills, "sold_count": len(sold_map)}
