import hashlib
import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import requests

from app.services.buff_auth import get_buff_auth_lock
from app.services.buff_egress import (
    BuffEgressBinding,
    BuffEgressReauthRequired,
    configure_requests_session,
    direct_buff_egress_binding,
    resolve_buff_egress,
    validate_buff_credential_binding,
)
from buff import (
    BuffBuyer,
    BuffWriteResultUnknown,
    PAY_METHOD_ALIPAY,
    PAY_METHOD_WECHAT,
)


logger = logging.getLogger(__name__)
buff_timeout = 15
BUFF_ACCOUNT_ID = "default"


def count_lowest_price_orders(orders: List[dict]) -> Tuple[float, int]:
    if not orders:
        return 0.0, 0
    lowest = float(orders[0].get("price", 0))
    if lowest <= 0:
        return 0.0, 0
    count = 0
    for order in orders:
        try:
            price = float(order.get("price", 0))
        except (ValueError, TypeError):
            continue
        if abs(price - lowest) < 1e-6:
            count += 1
        elif price < lowest:
            lowest = price
            count = 1
    return lowest, count


def first_order_at_price(orders: List[dict], price: float) -> Optional[dict]:
    for order in orders:
        try:
            order_price = float(order.get("price", 0))
        except (ValueError, TypeError):
            continue
        if abs(order_price - price) < 1e-6:
            return order
    return None


class BuffClient:
    """Generation-aware BUFF facade with one immutable authenticated egress."""

    supports_batch_buy = False

    def __init__(
        self,
        cookies: str,
        pay_method: str = "alipay",
        timeout_sec: int = buff_timeout,
        *,
        user_agent: Optional[str] = None,
        credential_generation: int = 0,
        credentials_provider: Optional[Callable[[], dict]] = None,
        credentials_update_callback: Optional[Callable[[str, str], None]] = None,
        egress_binding: Optional[BuffEgressBinding] = None,
        egress_binding_provider: Optional[Callable[[], BuffEgressBinding]] = None,
    ) -> None:
        self._pay_method = (pay_method or "alipay").strip().lower()
        self._pay_method_id = (
            PAY_METHOD_WECHAT if self._pay_method == "wechat" else PAY_METHOD_ALIPAY
        )
        self._timeout = timeout_sec
        self._credentials_provider = credentials_provider
        self._credentials_update_callback = credentials_update_callback
        self._credential_generation = self._as_generation(credential_generation)
        self._cookies = cookies or ""
        self._user_agent = (user_agent or "").strip() or None
        self._steam_id = ""
        self._client_lock = threading.RLock()
        self._auth_lock = get_buff_auth_lock()
        self._egress_binding = egress_binding or direct_buff_egress_binding()
        self._egress_binding_provider = egress_binding_provider
        self._buyer = self._new_buyer(self._cookies, self._user_agent)

    @staticmethod
    def _as_generation(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _current_egress_binding(self) -> BuffEgressBinding:
        current = (
            self._egress_binding_provider()
            if self._egress_binding_provider is not None
            else self._egress_binding
        )
        if (
            current.mode != self._egress_binding.mode
            or current.fingerprint != self._egress_binding.fingerprint
        ):
            raise BuffEgressReauthRequired("BUFF_EGRESS_REAUTH_REQUIRED")
        return current

    def _new_buyer(self, cookies: str, user_agent: Optional[str]) -> BuffBuyer:
        session = configure_requests_session(requests.Session(), self._egress_binding)
        buyer = BuffBuyer(
            cookies,
            pay_method=self._pay_method_id,
            user_agent=user_agent,
            session=session,
            account_id=BUFF_ACCOUNT_ID,
            request_timeout=self._timeout,
            steam_id=self._steam_id,
        )
        setattr(buyer, "_aetherswap_egress_session", session)
        return buyer

    @staticmethod
    def _close_buyer(buyer: BuffBuyer) -> None:
        try:
            close = getattr(buyer, "close", None)
            if callable(close):
                close()
        finally:
            session = getattr(buyer, "_aetherswap_egress_session", None)
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass

    def _ensure_current_buyer(self) -> BuffBuyer:
        current_binding = self._current_egress_binding()
        if self._credentials_provider is None:
            return self._buyer
        credentials = self._credentials_provider() or {}
        validate_buff_credential_binding(credentials, current_binding)
        generation = self._as_generation(credentials.get("generation"))
        cookies = str(credentials.get("cookies") or "")
        user_agent = str(credentials.get("user_agent") or "").strip() or None
        if (
            generation == self._credential_generation
            and cookies == self._cookies
            and user_agent == self._user_agent
        ):
            return self._buyer

        old_buyer = self._buyer
        self._buyer = self._new_buyer(cookies, user_agent)
        self._cookies = cookies
        self._user_agent = user_agent
        self._credential_generation = generation
        self._close_buyer(old_buyer)
        return self._buyer

    def _persist_rotated_cookies(self, buyer: BuffBuyer) -> None:
        if self._credentials_update_callback is None:
            return
        latest = buyer.export_cookie_string()
        if not latest or latest == self._cookies:
            return
        self._credentials_update_callback(latest, buyer.user_agent)
        self._cookies = latest
        if self._credentials_provider is not None:
            current = self._credentials_provider() or {}
            self._credential_generation = self._as_generation(
                current.get("generation")
            )

    def get_credential_identity(self) -> Dict[str, Any]:
        """Return a non-secret identity for checkout continuity checks."""

        with self._auth_lock:
            with self._client_lock:
                current_binding = self._current_egress_binding()
                if self._credentials_provider is not None:
                    credentials = self._credentials_provider() or {}
                    validate_buff_credential_binding(credentials, current_binding)
                    generation = self._as_generation(credentials.get("generation"))
                    cookies = str(credentials.get("cookies") or "")
                    user_agent = (
                        str(credentials.get("user_agent") or "").strip() or None
                    )
                else:
                    generation = self._credential_generation
                    cookies = self._cookies
                    user_agent = self._user_agent
                digest = hashlib.sha256(
                    f"{cookies}\0{user_agent or ''}".encode(
                        "utf-8", errors="replace"
                    )
                ).hexdigest()
                return {
                    "credential_generation": generation,
                    "credential_fingerprint": digest,
                }

    def _run(self, operation: Callable[[BuffBuyer], Any]) -> Any:
        with self._auth_lock:
            with self._client_lock:
                buyer = self._ensure_current_buyer()
                try:
                    return operation(buyer)
                finally:
                    try:
                        self._persist_rotated_cookies(buyer)
                    except Exception as exc:
                        logger.exception("持久化 BUFF 轮换 Cookie 失败: %s", exc)

    def close(self) -> None:
        with self._client_lock:
            self._close_buyer(self._buyer)

    def get_sell_orders(self, goods_id: int, game: str = "csgo") -> Optional[list]:
        return self._run(lambda buyer: buyer.get_sell_orders(goods_id, game))

    def verify_session(self, game: str = "csgo") -> bool:
        def operation(buyer: BuffBuyer) -> bool:
            verified = bool(buyer.verify_session(game))
            if verified and buyer.steam_id:
                self._steam_id = buyer.steam_id
            return verified

        return bool(self._run(operation))

    def get_steam_trades(self) -> Optional[list]:
        return self._run(lambda buyer: buyer.get_steam_trades())

    def get_buy_order_history_page(
        self, page_num: int, game: str = "csgo"
    ) -> dict:
        """Read one bounded BUFF buy-order history page through the facade."""

        return self._run(
            lambda buyer: buyer.get_buy_order_history_page(page_num, game)
        )

    def get_buy_orders_waiting_to_send_offer(
        self, game: str = "csgo", appid: int = 730
    ) -> Optional[list]:
        """Read buyer-send direction through the owned authenticated buyer."""

        return self._run(
            lambda buyer: buyer.get_buy_orders_waiting_to_send_offer(game, appid)
        )

    def send_buyer_offer(
        self,
        *,
        steam_cookie_string: str,
        buff_order_id: str,
        steam_id: str,
        timeout_seconds: float,
    ) -> dict:
        """Execute exactly one buyer-send transport call inside facade ownership."""

        from buff.buyer_send import BuffBuyerSendTransport

        return self._run(
            lambda buyer: BuffBuyerSendTransport(buyer).send(
                steam_cookie_string=steam_cookie_string,
                buff_order_id=buff_order_id,
                steam_id=steam_id,
                timeout_seconds=timeout_seconds,
            )
        )

    def get_goods_steam_price_cny(
        self, search_name: str, game: str = "csgo"
    ) -> Optional[float]:
        return self._run(
            lambda buyer: buyer.get_goods_steam_price_cny(search_name, game)
        )

    def ask_seller_to_send(
        self, bill_order_id_or_ids: Union[str, List[str]], game: str = "csgo"
    ) -> bool:
        return self._run(
            lambda buyer: buyer.ask_seller_to_send(bill_order_id_or_ids, game)
        )

    def lock_and_get_pay_url(
        self,
        game: str,
        goods_id: int,
        sell_order_id: str,
        price: str,
        *,
        on_created: Optional[Callable[[str], None]] = None,
        preview: Optional[dict] = None,
    ) -> Dict[str, Any]:
        return self._run(
            lambda buyer: buyer.lock_and_get_pay_url(
                game,
                goods_id,
                sell_order_id,
                price,
                on_created=on_created,
                preview=preview,
            )
        )

    def prepare_single_buy(
        self,
        game: str,
        goods_id: int,
        sell_order_id: str,
        price: str,
    ) -> Dict[str, Any]:
        """Complete BUFF's read-only preview before a checkout intent is written."""

        def operation(buyer: BuffBuyer) -> Dict[str, Any]:
            preview = buyer.preview_buy(game, goods_id, sell_order_id, price)
            if preview.get("code") != "OK":
                return {
                    "success": False,
                    "created": False,
                    "code": str(preview.get("code") or "PREVIEW_REJECTED"),
                    "msg": preview.get("error")
                    or preview.get("msg")
                    or "BUFF 购买预检未通过",
                }
            data = preview.get("data")
            if not isinstance(data, dict):
                return {
                    "success": False,
                    "created": False,
                    "code": "PREVIEW_INVALID",
                    "msg": "BUFF 购买预检返回格式异常",
                }
            error = buyer._preview_payment_error(data)
            if error:
                return {
                    "success": False,
                    "created": False,
                    "code": "PAY_METHOD_UNAVAILABLE",
                    "msg": error,
                }
            return {"success": True, "created": False, "preview": preview}

        return self._run(operation)

    def try_batch_buy(
        self,
        goods_id: int,
        game: str,
        orders: List[dict],
        unit_price: float,
        num: int,
        *,
        on_created: Optional[Callable[[str], None]] = None,
    ) -> Optional[Dict[str, Any]]:
        del goods_id, game, orders, unit_price, num, on_created
        return {
            "success": False,
            "code": "NOT_SUPPORTED",
            "created": False,
            "safe_to_fallback": True,
            "msg": "BUFF 批量购买协议已变化，已安全降级为单件购买",
        }

    def batch_buy_find_and_finalize(
        self,
        goods_id: int,
        game: str,
        max_price: float,
        num: int,
        batch_id: str,
        *,
        on_match: Optional[
            Callable[[Dict[str, Any], List[Dict[str, Any]]], None]
        ] = None,
    ) -> List[Dict[str, Any]]:
        def operation(buyer: BuffBuyer) -> List[Dict[str, Any]]:
            matched: List[Dict[str, Any]] = []
            seen_sell_order_ids = set()
            seen_bill_order_ids = set()
            try:
                orders = buyer.get_sell_orders(goods_id, game)
            except Exception as exc:
                setattr(exc, "partial_results", list(matched))
                setattr(exc, "batch_id", str(batch_id))
                raise
            if not orders:
                return []
            for order in orders:
                if len(matched) >= num:
                    break
                sell_order_id = str(order.get("id") or "").strip()
                if (
                    not sell_order_id
                    or sell_order_id == "0"
                    or sell_order_id in seen_sell_order_ids
                ):
                    continue
                try:
                    price = float(order.get("price", 0))
                except (ValueError, TypeError):
                    continue
                if price <= max_price:
                    seen_sell_order_ids.add(sell_order_id)
                    try:
                        bill_order_id = buyer.batch_buy_finalize(
                            game,
                            goods_id,
                            sell_order_id,
                            str(order.get("price", "")),
                            batch_id,
                        )
                    except Exception as exc:
                        setattr(exc, "partial_results", list(matched))
                        setattr(exc, "batch_id", str(batch_id))
                        raise
                    if bill_order_id:
                        normalized_bill_id = str(bill_order_id).strip()
                        if (
                            not normalized_bill_id
                            or normalized_bill_id == "0"
                            or normalized_bill_id in seen_bill_order_ids
                        ):
                            error = BuffWriteResultUnknown(
                                "BUFF 批量核销返回了空值或重复订单号，无法确认完整件数",
                                method="POST",
                            )
                            error.partial_results = list(matched)
                            error.batch_id = str(batch_id)
                            raise error
                        seen_bill_order_ids.add(normalized_bill_id)
                        match = {
                            "id": sell_order_id,
                            "price": price,
                            "bill_order_id": normalized_bill_id,
                        }
                        matched.append(match)
                        if on_match is not None:
                            try:
                                on_match(dict(match), list(matched))
                            except Exception as exc:
                                setattr(exc, "partial_results", list(matched))
                                setattr(exc, "batch_id", str(batch_id))
                                raise
                    else:
                        break
            return matched

        return self._run(operation)


def create_buff_client_from_config(credentials: dict, config: dict) -> BuffClient:
    from app.config_loader import (
        get_buff_credentials,
        load_app_config_validated,
        update_buff_creds,
    )

    credentials = credentials or {}
    buff_cfg = config.get("buff", {})
    binding = resolve_buff_egress(config)
    validate_buff_credential_binding(credentials, binding)

    def current_binding() -> BuffEgressBinding:
        latest = resolve_buff_egress(load_app_config_validated())
        if latest.mode != binding.mode or latest.fingerprint != binding.fingerprint:
            raise BuffEgressReauthRequired("BUFF_EGRESS_REAUTH_REQUIRED")
        return latest

    def persist_rotated_cookies(cookies: str, user_agent: str) -> None:
        # Preserve egress metadata and authentication source; only server-issued
        # CookieJar/UA rotation advances the credential generation here.
        update_buff_creds(cookies, user_agent=user_agent)

    return BuffClient(
        str(credentials.get("cookies") or ""),
        pay_method=buff_cfg.get("pay_method", "alipay"),
        timeout_sec=buff_timeout,
        user_agent=str(credentials.get("user_agent") or "").strip() or None,
        credential_generation=credentials.get("generation", 0),
        credentials_provider=get_buff_credentials,
        credentials_update_callback=persist_rotated_cookies,
        egress_binding=binding,
        egress_binding_provider=current_binding,
    )
