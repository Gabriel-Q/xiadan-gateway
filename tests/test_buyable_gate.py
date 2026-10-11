# -*- coding: utf-8 -*-
"""「可买/可卖」就绪门控单元测试

覆盖 _verify_buyable_ready 的读取竞速语义：
- 首读有值：零等待直通
- 首读为空、预算内填充：轮询重读后放行（2026-10-11 误拦缺陷回归）
- 预算耗尽仍空：SERVER_UNAVAILABLE
- wait_seconds=0：单次读取（旧语义）
- 控件缺失/读取异常：fail-open 放行（异常≠空）
- "0"：放行（资金/持仓不足走服务端语义）
"""
import time
import types

import pytest

from src.core.trader import Trader
from src.exceptions import ApiError, ErrorCode


class _Rect:
    def __init__(self, top):
        self.top = top


class FakeLabel:
    """1401 标签槽位"""

    def control_id(self):
        return 1401

    def rectangle(self):
        return _Rect(100)


class FakeValue:
    """同槽位数值 Image；readings 为 str/Exception 序列，末位复用"""

    def __init__(self, cid, top, readings):
        self._cid = cid
        self._top = top
        self._readings = list(readings)
        self.calls = 0

    def control_id(self):
        return self._cid

    def rectangle(self):
        return _Rect(self._top)

    def window_text(self):
        self.calls += 1
        r = self._readings[min(self.calls - 1, len(self._readings) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


class _Log:
    def __init__(self):
        self.infos = []
        self.warns = []

    def info(self, msg, *a, **k):
        self.infos.append(msg)

    def warning(self, msg, *a, **k):
        self.warns.append(msg)


def _make_trader(order_cfg, label=FakeLabel()):
    """绕过 __init__ 直接装配门控依赖（window_service/config/logger）"""
    trader = object.__new__(Trader)
    trader.logger = _Log()
    trader.config = types.SimpleNamespace(
        get_order_config=lambda: dict(order_cfg))
    trader.window_service = types.SimpleNamespace(
        find_element_in_window=lambda window, cid, descendants=None: label)
    return trader


def _run(trader, descendants, value_cid=1018, field="可买"):
    return trader._verify_buyable_ready(None, descendants, value_cid, field)


def test_first_read_ready_zero_wait():
    """首读有值：单次读取直通，不引入等待"""
    v = FakeValue(1018, 100, ["2300"])
    t = _make_trader({})
    start = time.time()
    _run(t, [v])
    assert v.calls == 1
    assert time.time() - start < 0.2


def test_async_fill_within_budget_passes():
    """首读空、预算内填充：轮询重读放行（竞速误拦回归）"""
    v = FakeValue(1018, 100, ["", "", "2300"])
    t = _make_trader({"verify_buyable_ready_wait_seconds": 1.0})
    start = time.time()
    _run(t, [v])
    assert v.calls >= 3
    assert time.time() - start < 1.0
    assert any("等待填充" in m for m in t.logger.infos)


def test_never_fills_raises_after_budget():
    """预算耗尽仍空：SERVER_UNAVAILABLE，且确实等满了预算"""
    v = FakeValue(1018, 100, [""])
    t = _make_trader({"verify_buyable_ready_wait_seconds": 0.3})
    start = time.time()
    with pytest.raises(ApiError) as ei:
        _run(t, [v])
    assert ei.value.error_code == ErrorCode.SERVER_UNAVAILABLE
    assert ei.value.details["wait_seconds"] > 0
    assert 2 <= v.calls <= 4
    assert 0.25 <= time.time() - start < 1.0


def test_zero_wait_single_read():
    """wait_seconds=0：单次读取即拦（旧语义）"""
    v = FakeValue(1018, 100, [""])
    t = _make_trader({"verify_buyable_ready_wait_seconds": 0})
    with pytest.raises(ApiError) as ei:
        _run(t, [v])
    assert ei.value.error_code == ErrorCode.SERVER_UNAVAILABLE
    assert v.calls == 1


def test_missing_label_passes():
    """标签槽位缺失：放行"""
    t = _make_trader({}, label=None)
    _run(t, [])  # 不抛即通过


def test_value_control_missing_passes():
    """同槽位无数值控件：放行"""
    t = _make_trader({})
    _run(t, [])  # descendants 为空，找不到 cid=1018


def test_read_exception_passes():
    """读取异常≠空：fail-open 放行（元素失效场景）"""
    v = FakeValue(1018, 100, [RuntimeError("stale element")])
    t = _make_trader({})
    _run(t, [v])


def test_reread_exception_passes():
    """重读阶段异常同样放行：异常不落入拦截"""
    v = FakeValue(1018, 100, ["", RuntimeError("stale element")])
    t = _make_trader({"verify_buyable_ready_wait_seconds": 0.5})
    _run(t, [v])


def test_zero_value_passes():
    """"0" 不拦：资金/持仓不足走服务端既有语义"""
    v = FakeValue(1038, 100, ["0"])
    t = _make_trader({})
    _run(t, [v], value_cid=1038, field="可卖")


def test_sell_field_locates_by_cid():
    """卖出页以 cid=1038 同行锚定（标签 1401 共用）"""
    v = FakeValue(1038, 100, ["2300"])
    other = FakeValue(1018, 100, ["9999"])  # 同行买入页控件，不应被选中
    t = _make_trader({})
    _run(t, [other, v], value_cid=1038, field="可卖")
    assert v.calls == 1 and other.calls == 0
