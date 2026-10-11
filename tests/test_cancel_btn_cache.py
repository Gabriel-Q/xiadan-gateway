# -*- coding: utf-8 -*-
"""撤单按钮缓存单元测试

cancel_all_orders 快路径：类级缓存命中(同根句柄+cid)时 F3 后直接点击缓存
按钮，免"挡路弹窗检查+找按钮"两次全树遍历；灰显(未启用)回退全遍历复核；
遍历合并为一次(挡路检查与找按钮共用同一棵树快照)。
"""
import types

import pytest

from src.services.trading_service import TradingService


class FakeEI:
    def __init__(self, control_type):
        self.control_type = control_type


class FakeButton:
    def __init__(self, cid=30001, enabled_seq=(True,), text="全部撤单"):
        self._cid = cid
        self._enabled = list(enabled_seq)
        self._text = text
        self.clicks = 0

    def control_id(self):
        return self._cid

    def window_text(self):
        return self._text

    def is_enabled(self):
        if len(self._enabled) > 1:
            return self._enabled.pop(0)
        return self._enabled[0]

    def click_input(self):
        self.clicks += 1


class FakeWindow:
    def __init__(self, handle=1, nodes=None):
        self.handle = handle
        self._nodes = list(nodes or [])
        self.descendants_calls = 0

    def set_nodes(self, nodes, once=False):
        """替换树内容；once=True 时提供一次后清空(模拟不可重入遍历)"""
        self._nodes = list(nodes)
        self._once = once

    def descendants(self):
        self.descendants_calls += 1
        d, self._nodes = self._nodes, ([] if getattr(self, "_once", False)
                                       else self._nodes)
        return d


def _make_service(find_btn=None):
    """绕过 __init__ 装配；find_btn(window, cid, descendants) 模拟按钮查找"""
    svc = object.__new__(TradingService)
    svc.logger = types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None)
    svc.config = types.SimpleNamespace()
    svc._had_dialog = False
    calls = {"get_trading_window": 0, "get_trading_window_fast": 0,
             "find": 0, "click_element": 0}

    def get_trading_window(*a, **k):
        calls["get_trading_window"] += 1
        return svc._window

    def find(window, cid, descendants=None):
        calls["find"] += 1
        return find_btn(window, cid, descendants) if find_btn else None

    svc.window_service = types.SimpleNamespace(
        get_trading_window=get_trading_window,
        get_trading_window_fast=lambda *a, **k: None,
        find_element_in_window=find,
        send_key=lambda *a, **k: None,
        click_element=lambda *a, **k: calls.__setitem__(
            "click_element", calls["click_element"] + 1),
    )
    svc._calls = calls
    return svc


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """隔离:类缓存重置 + report_window_state/broker_link 外部依赖"""
    TradingService._cancel_btn_cache = None
    monkeypatch.setattr("src.services.trading_service.report_window_state",
                        lambda f: f)
    monkeypatch.setattr("src.services.broker_link.check_after_order",
                        lambda *a, **k: None)
    yield
    TradingService._cancel_btn_cache = None


def test_fast_path_clicks_cached_button_without_scan(monkeypatch):
    """缓存命中且启用:直接点击缓存按钮,零全树遍历、零 find"""
    main = FakeWindow(handle=1)
    main.descendants = main.descendants  # 占位;调用即失败由 find 抛错代替
    btn = FakeButton(30001, enabled_seq=(True,))
    svc = _make_service(find_btn=lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("快路径命中时不应发生全树查找")))
    svc._window = main
    TradingService._cancel_btn_cache = {1: {30001: btn}}
    monkeypatch.setattr(svc, "_has_blocking_text",
                        staticmethod(lambda d: (_ for _ in ()).throw(
                            AssertionError("快路径不应做挡路弹窗检查"))))

    result = svc.cancel_all_orders("A")
    assert result["success"] is True
    assert btn.clicks == 1
    assert main.descendants_calls == 0  # 一次遍历都没发生


def test_fallback_merges_two_traversals_into_one_and_records(monkeypatch):
    """无缓存:挡路检查与找按钮合并为一次遍历,命中后记录缓存"""
    main = FakeWindow(handle=1)
    btn = FakeButton(30001, enabled_seq=(True,))
    main.set_nodes([btn])
    seen = []
    svc = _make_service(find_btn=lambda w, cid, descendants=None: (
        seen.append(descendants),
        btn if (descendants and cid == 30001) else None)[1])
    monkeypatch.setattr(svc, "_has_blocking_text", staticmethod(lambda d: False))
    svc._window = main

    result = svc.cancel_all_orders("A")
    assert result["success"] is True
    assert main.descendants_calls == 1  # 原实现为 2(检查+查找各一次)
    assert seen[0] is not None          # find 复用了同一棵树快照
    assert TradingService._cancel_btn_cache[1][30001] is btn


def test_not_enabled_cached_falls_back_to_full_scan(monkeypatch):
    """缓存按钮未启用(可能页面未渲染完):不点击,回退全遍历复核"""
    main = FakeWindow(handle=1)
    cached_btn = FakeButton(30001, enabled_seq=(False, False))
    fresh_btn = FakeButton(30001, enabled_seq=(False,))
    main.set_nodes([fresh_btn])
    svc = _make_service(find_btn=lambda w, cid, descendants=None: fresh_btn)
    svc._window = main
    TradingService._cancel_btn_cache = {1: {30001: cached_btn}}
    monkeypatch.setattr(svc, "_has_blocking_text", staticmethod(lambda d: False))

    result = svc.cancel_all_orders("A")
    assert cached_btn.clicks == 0          # 未启用不点击
    assert result["success"] is False      # 复核后仍灰显 → 无可撤委托
    assert result["reason"] == "当前无可撤委托"
    assert TradingService._cancel_btn_cache[1][30001] is fresh_btn  # 重录


def test_cache_isolated_per_root_handle(monkeypatch):
    """主窗口重建(句柄变):旧句柄缓存不命中,走全遍历"""
    main = FakeWindow(handle=2)
    btn = FakeButton(30001, enabled_seq=(True,))
    main.set_nodes([btn])
    svc = _make_service(find_btn=lambda w, cid, descendants=None: btn)
    monkeypatch.setattr(svc, "_has_blocking_text", staticmethod(lambda d: False))
    svc._window = main
    TradingService._cancel_btn_cache = {1: {30001: FakeButton(30001)}}

    result = svc.cancel_all_orders("A")
    assert result["success"] is True
    assert main.descendants_calls == 1     # 未命中缓存,走了合并遍历
    assert TradingService._cancel_btn_cache[2][30001] is btn


def test_second_cancel_uses_recorded_cache(monkeypatch):
    """第一次撤单记录缓存后,第二次同根句柄走快路径"""
    main = FakeWindow(handle=1)
    btn = FakeButton(30001, enabled_seq=(True,))
    main.set_nodes([btn])
    svc = _make_service(find_btn=lambda w, cid, descendants=None: btn)
    monkeypatch.setattr(svc, "_has_blocking_text", staticmethod(lambda d: False))
    svc._window = main

    svc.cancel_all_orders("A")
    first_calls = main.descendants_calls
    main.set_nodes([])  # 不再提供树——第二次若仍遍历,find 将拿空列表
    svc.cancel_all_orders("A")
    assert btn.clicks == 2
    assert first_calls == 1
