"""查询类路由 Blueprint

包含: 资金余额、持仓、今日成交、当日委托
"""
import time

from flask import Blueprint

from src.api.helpers import get_param, fast_mode_enabled, strip_blank_columns
from src.api.response import (
    generate_request_id, success_response, error_response_from_exception
)
from src.api.task_queue import TaskQueue
from src.models.config import AppConfig

query_bp = Blueprint("query", __name__)


def _get_position_service():
    """延迟获取 PositionService（避免循环依赖）"""
    from src.core.ocr import OcrService
    from src.services.position_service import PositionService
    from src.services.window_service import WindowService
    return PositionService(WindowService(), OcrService.get_instance())


def _get_fast_trader():
    """延迟获取 FastTrader（极速映射分支用，避免循环依赖）"""
    from src.core.ocr import OcrService
    from src.services.fast_trader import FastTrader
    return FastTrader(OcrService.get_instance())


def _submit_query(func, task_name: str, timeout: int, params: dict = None):
    """排队执行查询任务：前置自愈（断开→F5 戳重连）+ 事后断开复查

    查询链路（F4+Ctrl+C）不发网络请求、无法触发客户端重连（2026-10-10
    三轮断网实测）——前置 poke_if_disconnected 在工作线程内读状态格，
    断开在场则 F5（防抖 30s）并等重连（≤3s）再查询；查询结束后复查，
    仍断开则结果作废抛 BROKER_DISCONNECTED（缓存旧值不交付）。
    """
    def _task():
        from src.services.broker_link import poke_if_disconnected
        poke_if_disconnected()
        return func()

    result = TaskQueue.get_instance().submit(
        func=_task, task_name=task_name, params=params or {},
        timeout=timeout)
    from src.services.broker_link import ensure_connected_or_discard
    ensure_connected_or_discard("查询")
    return result


@query_bp.route("/account/balance", methods=["GET"])
def get_balance():
    """获取资金余额

    order.fast_mode=true: 映射极速路径（资金股票页 cid 直读，无验证码，~1.9s）；
    false: 传统流程。两者响应键完全一致（同为 BALANCE_FIELDS 读取）。
    """
    request_id = generate_request_id()
    _start = time.time()
    config = AppConfig()
    try:
        if fast_mode_enabled():
            from src.services.fast_trader import FastTrader
            result = TaskQueue.get_instance().submit(
                func=lambda: FastTrader().query_balance(),
                task_name="fast_balance", timeout=20)
            return success_response(result, request_id, duration_ms=(time.time() - _start) * 1000)
        query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 30)
        result = _submit_query(
            lambda: _get_position_service().get_balance(),
            task_name="get_balance", timeout=query_timeout)
        return success_response(result, request_id, duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)


@query_bp.route("/positions", methods=["GET"])
def get_position():
    """获取当前持仓

    order.fast_mode=true: 映射极速路径（双向委托页 F6 页内查询，~2.3s；
    丢弃空占位列，响应为行字典列表，契约与传统一致）。断连时极速路径
    无 BROKER_DISCONNECTED 作废语义（开启极速即接受，2026-10-12 用户定版）。
    false: 传统流程（资金股票页 + 特征列验证 + 断连作废）。
    """
    request_id = generate_request_id()
    _start = time.time()
    config = AppConfig()
    try:
        if fast_mode_enabled():
            rows = TaskQueue.get_instance().submit(
                func=lambda: _get_fast_trader().query_tab("positions"),
                task_name="fast_query_positions", timeout=20)
            return success_response(strip_blank_columns(rows), request_id,
                                    duration_ms=(time.time() - _start) * 1000)
        query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 30)
        result = _submit_query(
            lambda: _get_position_service().get_position(),
            task_name="get_position", timeout=query_timeout)
        return success_response(result, request_id, duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)


@query_bp.route("/trades/today", methods=["GET"])
def get_today_trades():
    """获取今日成交

    order.fast_mode=true: 映射极速路径（F7 页内查询，~2.3s）；false: 传统流程。
    """
    request_id = generate_request_id()
    _start = time.time()
    config = AppConfig()
    try:
        if fast_mode_enabled():
            rows = TaskQueue.get_instance().submit(
                func=lambda: _get_fast_trader().query_tab("trades"),
                task_name="fast_query_trades", timeout=20)
            return success_response(strip_blank_columns(rows), request_id,
                                    duration_ms=(time.time() - _start) * 1000)
        query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 30)
        result = _submit_query(
            lambda: _get_position_service().get_today_trades(),
            task_name="get_today_trades", timeout=query_timeout)
        return success_response(result, request_id, duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)


@query_bp.route("/orders/<entrust_no>/status", methods=["GET"])
def get_order_status(entrust_no: str):
    """按合同编号查询委托状态与成交回报

    合同编号 join 当日委托 × 当日成交：返回委托状态（由数量推导）、
    成交聚合（数量/金额/加权均价）与逐笔成交编号列表。
    found=false 表示当日委托中无此编号（非当日委托或编号有误）。
    流程: 当日委托查询 → 命中后当日成交查询 → 纯函数聚合（一次排队）。
    """
    request_id = generate_request_id()
    _start = time.time()
    config = AppConfig()
    try:
        if not (entrust_no.isdigit() and 6 <= len(entrust_no) <= 24):
            from src.exceptions import ApiError, ErrorCode
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                f"entrust_no 格式错误: {entrust_no}",
                suggestion="合同编号为 6-24 位纯数字（不同券商/交易所长度不同）")
        query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 30)
        result = _submit_query(
            lambda: _get_position_service().get_order_status(entrust_no),
            task_name="get_order_status",
            timeout=query_timeout * 2,  # 复合查询：最多两次表格复制
            params={"entrust_no": entrust_no})
        return success_response(result, request_id, duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)


@query_bp.route("/orders/pending", methods=["GET"])
def get_today_orders():
    """获取在途委托（语义归正 2026-10-12：pending = 在途）

    order.fast_mode=true: 极速 F8「可撤委托」页——天然只含在途单（撤掉即
    从列表消失），~2.3s。false: 保持历史行为不变——传统「当日委托」页
    无在途过滤，实际返回当日全量（含已成交/已撤；名实差异见 /orders/full）。
    全量委托两态统一走 /orders/full。
    """
    request_id = generate_request_id()
    _start = time.time()
    config = AppConfig()
    try:
        if fast_mode_enabled():
            rows = TaskQueue.get_instance().submit(
                func=lambda: _get_fast_trader().query_tab("orders"),
                task_name="fast_query_orders", timeout=20)
            return success_response(strip_blank_columns(rows), request_id,
                                    duration_ms=(time.time() - _start) * 1000)
        query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 30)
        result = _submit_query(
            lambda: _get_position_service().get_today_orders(),
            task_name="get_today_orders", timeout=query_timeout)
        return success_response(result, request_id, duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)


@query_bp.route("/orders/full", methods=["GET"])
def get_full_orders():
    """获取当日全量委托（含已成交/已撤/废单）——全量委托唯一入口

    order.fast_mode=true: 映射极速路径（F4「当日委托」页，~4s）；false:
    传统「当日委托」页流程（与 /orders/pending 的传统实现同源）。两态
    语义一致；表头无独立状态列——状态经「备注」（如"全部撤单"）与
    「撤消数量/成交数量」体现。
    """
    request_id = generate_request_id()
    _start = time.time()
    config = AppConfig()
    try:
        if fast_mode_enabled():
            rows = TaskQueue.get_instance().submit(
                func=lambda: _get_fast_trader().query_full_orders(),
                task_name="fast_full_orders", timeout=25)
            return success_response(strip_blank_columns(rows), request_id,
                                    duration_ms=(time.time() - _start) * 1000)
        query_timeout = config.get_task_queue_config().get("query_timeout_seconds", 30)
        result = _submit_query(
            lambda: _get_position_service().get_today_orders(),
            task_name="get_today_orders", timeout=query_timeout)
        return success_response(result, request_id, duration_ms=(time.time() - _start) * 1000)
    except Exception as e:
        return error_response_from_exception(e, request_id)
