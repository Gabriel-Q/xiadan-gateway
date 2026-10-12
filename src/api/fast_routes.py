"""极速模式路由（/fast/*）——双向委托单页流

与旧端点共用 TaskQueue 单 worker（UI 资源串行铁律）与 token 鉴权；
无幂等键/无防重（用户定版最简路径，重试保护由策略层负责）。
"""
import time

from flask import Blueprint, request

from src.api.helpers import get_param
from src.api.response import (
    generate_request_id,
    success_response, error_response_from_exception
)
from src.api.task_queue import TaskQueue
from src.core.ocr import OcrService
from src.core.validation import sanitize_price
from src.exceptions import ApiError, ErrorCode
from src.models.config import AppConfig
from src.services.fast_trader import FastTrader

fast_bp = Blueprint("fast", __name__)

_VIEWS = {"positions": "持仓", "trades": "成交", "orders": "委托(可撤)"}
_CANCELS = ("A", "X", "C")


def _validate_order_body():
    """最小参数校验：code/status 必填，限价 price 两位小数，amount 正整数串"""
    code = (get_param("code") or "").strip()
    status = (get_param("status") or "").strip()
    amount = (get_param("amount") or "").strip()
    price = get_param("price")
    price_type = (get_param("price_type") or "limit").strip().lower()
    if not code:
        raise ApiError(ErrorCode.VALIDATION_ERROR, "code 参数不能为空")
    if status not in ("1", "2"):
        raise ApiError(ErrorCode.VALIDATION_ERROR,
                       "status 只能是 1(买入) 或 2(卖出)")
    if price_type not in ("limit", "market"):
        raise ApiError(ErrorCode.VALIDATION_ERROR,
                       f"price_type 只能是 limit(限价) 或 market(市价): {price_type!r}")
    if not amount or not amount.isdigit() or int(amount) <= 0:
        raise ApiError(ErrorCode.VALIDATION_ERROR,
                       f"amount 无效（正整数股数）: {amount!r}")
    if price_type == "limit":
        if not price:
            raise ApiError(ErrorCode.VALIDATION_ERROR,
                           "限价单 price 必填")
        try:
            price = sanitize_price(price)
        except Exception:
            raise ApiError(ErrorCode.VALIDATION_ERROR,
                           f"价格格式无效: {price!r}")
    elif price:
        raise ApiError(ErrorCode.VALIDATION_ERROR,
                       "市价单不能指定 price")
    return code, status, amount, price, price_type


@fast_bp.route("/fast/home", methods=["POST"])
def fast_home():
    """引导：导航到双向委托页并确保三个勾选框全部未勾选（幂等可重放）"""
    request_id = generate_request_id()
    task_queue = TaskQueue.get_instance()
    _start = time.time()
    try:
        result = task_queue.submit(
            func=lambda: FastTrader().bootstrap(),
            task_name="fast_bootstrap", timeout=30)
        return success_response(result, request_id,
                                duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)


@fast_bp.route("/fast/orders", methods=["POST"])
def fast_order():
    """极速下单：限价单全走双向委托页单页流（唯一门控=可买/可卖≥0）；
    市价单本页不支持，自动回落传统 Trader 流程（F1/F2 页，历史代码）。"""
    request_id = generate_request_id()
    task_queue = TaskQueue.get_instance()
    _start = time.time()
    try:
        code, status, amount, price, price_type = _validate_order_body()
        if price_type == "market":
            # 市价回落：双向委托页仅支持限价，跳传统流程（其自带全套防御）
            from src.core.trader import Trader
            from src.services.window_service import WindowService
            result = task_queue.submit(
                func=lambda: Trader(WindowService()).place_order(
                    code=code, status=status, amount=amount,
                    price=None, price_type="market", confirm=True),
                task_name="fast_market_fallback",
                params={"code": code, "status": status, "amount": amount},
                timeout=30)
            result["routed"] = "legacy(market)"
            return success_response(result, request_id,
                                    duration_ms=(time.time() - _start) * 1000)
        result = task_queue.submit(
            func=lambda: FastTrader(OcrService.get_instance()).place_order(
                code=code, status=status, price=price, amount=amount),
            task_name="fast_place_order",
            params={"code": code, "status": status,
                    "amount": amount, "price": price},
            timeout=15)
        return success_response(result, request_id,
                                duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)


@fast_bp.route("/fast/cancel", methods=["POST"])
def fast_cancel():
    """极速撤单：A=全撤 / X=撤买 / C=撤卖（同页按钮直点，无确认框）"""
    request_id = generate_request_id()
    task_queue = TaskQueue.get_instance()
    _start = time.time()
    try:
        cancel_type = (get_param("type") or "A").strip().upper()
        if cancel_type not in _CANCELS:
            raise ApiError(ErrorCode.VALIDATION_ERROR,
                           f"type 只能是 A(全部)/X(撤买)/C(撤卖): {cancel_type!r}")
        result = task_queue.submit(
            func=lambda: FastTrader().cancel(cancel_type),
            task_name="fast_cancel",
            params={"type": cancel_type},
            timeout=15)
        return success_response(result, request_id,
                                duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)


@fast_bp.route("/fast/<view>", methods=["GET"])
def fast_query(view):
    """页内查询：/fast/positions(F6 持仓) /fast/trades(F7 成交) /fast/orders(F8 可撤委托)

    F8 在「只显示可撤委托」勾选（引导强制）下只返回当前可撤的活单，
    撤掉即从列表消失；全量委托（含已撤）请走 /fast/orders/full
    （F4「当日委托」页，用户定版 2026-10-12）。
    """
    request_id = generate_request_id()
    task_queue = TaskQueue.get_instance()
    _start = time.time()
    if view not in _VIEWS:
        return error_response(
            ErrorCode.VALIDATION_ERROR,
            f"未知查询视图: {view}（可选 positions/trades/orders）",
            request_id)
    try:
        rows = task_queue.submit(
            func=lambda: FastTrader(OcrService.get_instance()).query_tab(view),
            task_name=f"fast_query_{view}",
            params={"view": view},
            timeout=20)
        return success_response({"view": _VIEWS[view], "rows": rows,
                                 "count": len(rows)},
                                request_id,
                                duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)


@fast_bp.route("/fast/balance", methods=["GET"])
def fast_balance():
    """资金余额：F4 树导航「资金股票」→ cid 批量读取（无验证码）"""
    request_id = generate_request_id()
    task_queue = TaskQueue.get_instance()
    _start = time.time()
    try:
        result = task_queue.submit(
            func=lambda: FastTrader().query_balance(),
            task_name="fast_balance", params={}, timeout=20)
        return success_response(result, request_id,
                                duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)


@fast_bp.route("/fast/orders/full", methods=["GET"])
def fast_full_orders():
    """当日全量委托（F4「当日委托」页，含已撤）——全量委托唯一查询口

    用户定版 2026-10-12：F8 只看可撤委托，完整委托列表一律走本端点。
    """
    request_id = generate_request_id()
    task_queue = TaskQueue.get_instance()
    _start = time.time()
    try:
        rows = task_queue.submit(
            func=lambda: FastTrader(OcrService.get_instance()).query_full_orders(),
            task_name="fast_full_orders", params={}, timeout=25)
        return success_response({"view": "当日委托(全量)", "rows": rows,
                                 "count": len(rows)},
                                request_id,
                                duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)
