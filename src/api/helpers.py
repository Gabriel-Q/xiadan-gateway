"""API 层共享工具函数"""
from typing import Optional

from flask import request


def get_param(name: str, default: Optional[str] = None) -> Optional[str]:
    """统一参数获取：优先 JSON body，回退 query string，最后 form body

    三级通道（优先级高 → 低）：
        1. JSON body —— force 解析，不依赖 Content-Type：curl -d '{"type":"X"}'
           不带头时 curl 缺省按 application/x-www-form-urlencoded 发送，
           只看 Content-Type 会把整个 body 静默丢弃（撤单 type 落回默认
           A，范围被静默扩大——README 早期示例即踩此坑）
        2. query string（?type=X）
        3. form body（urlencoded / multipart，curl -d type=X 的原生格式）

    Args:
        name: 参数名
        default: 默认值

    Returns:
        参数值字符串，未找到返回 default
    """
    body = request.get_json(silent=True, force=True)
    if isinstance(body, dict) and name in body:
        val = body.get(name)
        return val if val is None else str(val)
    val = request.args.get(name)
    if val is not None:
        return val
    return request.form.get(name, default)


def fast_mode_enabled() -> bool:
    """极速模式总开关（order.fast_mode，按请求读取 → /admin/reload-config 热重载即效）

    true 时传统查询/撤单端点在路由层映射到极速单页流（双向委托页，
    见 query_routes/order_routes 各 handler 分支）；false 时传统路径
    一行不改（严格隔离）。
    """
    from src.models.config import AppConfig
    return bool(AppConfig().get_order_config().get("fast_mode"))


def strip_blank_columns(rows: list) -> list:
    """丢弃键名为空白串的占位列（极速网格 TSV 首列=行选择区，解析为 ""）

    仅用于传统端点的极速映射分支：让映射后响应贴近传统列结构
    （传统侧无此空列）；/fast/* 原生端点契约不变，不做此处理。
    """
    return [{k: v for k, v in row.items() if str(k).strip()} for row in rows]
