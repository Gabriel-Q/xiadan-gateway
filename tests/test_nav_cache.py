# -*- coding: utf-8 -*-
"""导航缓存单元测试

_navigate_to_query_page 快路径：类级缓存命中(同根句柄+页名)时免全树
descendants 遍历——已在目标页跳过点击；点击后 is_selected 验证；
引用失效回退全遍历并重录。消息级复制模式下缓存须含有效网格 hwnd。
"""
import types

import pytest

from src.constants import GRID_CLASS_NAME
from src.services.position_service import PositionService


class FakeEI:
    def __init__(self, control_type):
        self.control_type = control_type


class FakeNode:
    """最小控件假件：类型/文本/类名/句柄/cid/父子树"""

    def __init__(self, ct="Pane", text="", cid=0, cls="Pane",
                 handle=1, children=None):
        self.element_info = FakeEI(ct)
        self._text = text
        self._cid = cid
        self._cls = cls
        self.handle = handle
        self._children = children or []

    def control_id(self):
        return self._cid

    def class_name(self):
        return self._cls

    def window_text(self):
        return self._text

    def children(self):
        return self._children


class FakeTreeItem(FakeNode):
    """TreeItem 假件：is_selected 按序列弹出(末位复用)，记录点击次数"""

    def __init__(self, text, selected_sequence=(True,)):
        super().__init__(ct="TreeItem", text=text)
        self._seq = list(selected_sequence)
        self.clicks = 0

    def click_input(self):
        self.clicks += 1

    def is_selected(self):
        if len(self._seq) > 1:
            return self._seq.pop(0)
        return self._seq[0]


class FakeMainWindow:
    """主窗口假件：descendants 可配置(用后即焚模拟不可重入遍历)"""

    def __init__(self, handle=1):
        self.handle = handle
        self._descendants = []

    def set_descendants(self, nodes, once=False):
        self._descendants = list(nodes)
        self._once = once

    def descendants(self):
        if self._once:
            d, self._descendants = self._descendants, []
            return d
        return self._descendants


def _make_service(copy_method="keyboard"):
    svc = object.__new__(PositionService)
    svc.logger = types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None)
    svc.config = types.SimpleNamespace(
        get_query_config=lambda: {"copy_method": copy_method})
    svc._cached_grid_hwnd = None
    return svc


def _tree_with_page(page_name="当日委托"):
    """主窗口 → 查询面板 → Tree → 查询[F4] → 页项；网格 1047 同树"""
    page_item = FakeTreeItem(page_name, selected_sequence=(True,))
    parent_item = FakeNode(ct="TreeItem", text="查询[F4]",
                           children=[page_item])
    tree = FakeNode(ct="Tree", children=[parent_item])
    grid = FakeNode(ct="Pane", cid=1047, cls=GRID_CLASS_NAME, handle=0x10B6E)
    main = FakeMainWindow(handle=1)
    main.set_descendants([tree, page_item, parent_item, grid])
    return main, page_item, grid


def setup_function(_):
    PositionService._nav_root_handle = None
    PositionService._nav_tree_items = None
    PositionService._nav_grid_hwnds = None


def teardown_function(_):
    PositionService._nav_root_handle = None
    PositionService._nav_tree_items = None
    PositionService._nav_grid_hwnds = None


def test_store_then_lookup_hit_keyboard_mode():
    """键盘模式:仅树项即可命中,hwnd 可缺省"""
    main, page_item, _ = _tree_with_page()
    svc = _make_service("keyboard")
    svc._cached_grid_hwnd = 0x10B6E
    svc._navigate_to_query_page(main, "当日委托")  # 首次:全遍历+记录
    hit = svc._nav_cache_lookup(main, "当日委托")
    assert hit is not None and hit[0] is page_item


def test_lookup_other_root_handle_rejected():
    """主窗口重建(句柄变):缓存按失效处理"""
    main, _, _ = _tree_with_page()
    svc = _make_service("keyboard")
    svc._navigate_to_query_page(main, "当日委托")
    other = FakeMainWindow(handle=99)
    assert svc._nav_cache_lookup(other, "当日委托") is None


def test_message_copy_requires_valid_hwnd(monkeypatch):
    """消息级复制:缓存缺网格 hwnd 或 hwnd 失效均不命中"""
    import win32gui
    main, _, _ = _tree_with_page()
    svc = _make_service("message")
    svc._cached_grid_hwnd = 0x10B6E
    svc._navigate_to_query_page(main, "当日委托")
    assert svc._nav_cache_lookup(main, "当日委托") is not None

    monkeypatch.setattr(win32gui, "IsWindow", lambda h: False)
    assert svc._nav_cache_lookup(main, "当日委托") is None

    svc2 = _make_service("message")  # 同树新实例(类缓存共享)但删掉 hwnd
    PositionService._nav_grid_hwnds = {}
    assert svc2._nav_cache_lookup(main, "当日委托") is None


def test_navigate_fast_path_skip_click_when_already_selected():
    """已在目标页:缓存命中跳过树点击,消息模式下顺带复用网格句柄"""
    main, page_item, _ = _tree_with_page()
    page_item._seq = [True]  # 恒选中
    svc = _make_service("message")
    PositionService._nav_root_handle = main.handle
    PositionService._nav_tree_items = {"当日委托": page_item}
    PositionService._nav_grid_hwnds = {"当日委托": 0x10B6E}
    main.set_descendants([])  # 快路径不得触发全树遍历

    svc._cached_grid_hwnd = None
    svc._navigate_to_query_page(main, "当日委托")
    assert page_item.clicks == 0
    assert svc._cached_grid_hwnd == 0x10B6E


def test_navigate_fast_path_click_and_verify():
    """未选中:缓存树项点击一次并验证选中,不触发全树遍历"""
    main, page_item, _ = _tree_with_page()
    page_item._seq = [False, True]
    svc = _make_service("keyboard")
    PositionService._nav_root_handle = main.handle
    PositionService._nav_tree_items = {"当日委托": page_item}
    main.set_descendants([])  # 全遍历若被触发会拿到空列表导致策略全败

    svc._navigate_to_query_page(main, "当日委托")
    assert page_item.clicks == 1


def test_navigate_fallback_to_full_scan_and_record():
    """缓存树项失效(点击不选中):回退全树扫描重录缓存,下次走快路径"""
    main, page_item, _ = _tree_with_page()
    stale = FakeTreeItem("当日委托", selected_sequence=(False, False))
    svc = _make_service("keyboard")
    PositionService._nav_root_handle = main.handle
    PositionService._nav_tree_items = {"当日委托": stale}
    main.set_descendants([FakeNode(ct="Tree", children=[
        FakeNode(ct="TreeItem", text="查询[F4]", children=[page_item])],
    )], once=True)

    svc._navigate_to_query_page(main, "当日委托")  # 快路径失败→全遍历重录
    assert page_item.clicks == 1
    assert PositionService._nav_tree_items["当日委托"] is page_item

    main.set_descendants([])  # 第二次:不得再遍历
    svc._navigate_to_query_page(main, "当日委托")
    assert page_item.clicks == 1  # 快路径:已选中跳过点击


def test_navigate_second_visit_same_page_skips_click():
    """同页连续导航:第二次缓存命中且已选中,零点击"""
    main, page_item, _ = _tree_with_page()
    svc = _make_service("keyboard")
    svc._navigate_to_query_page(main, "当日委托")   # 首次:全遍历+点击
    main.set_descendants([])  # 之后不再提供树
    svc._navigate_to_query_page(main, "当日委托")   # 快路径:已选中跳过
    assert page_item.clicks == 1
