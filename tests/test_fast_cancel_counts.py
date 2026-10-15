# -*- coding: utf-8 -*-
"""F8 可撤委托行数 vs 撤单按钮实时计数交叉校验测试（2026-10-15 定版）

按钮计数是客户端自维护的实时 UI 状态，与网格复制管线独立——网格复制到
陈旧数据（下单/撤单后页面刷新滞后）时两者必不一致，报 DATA_MISMATCH
由调用方走全量查询复核。按钮文本形态（2026-10-15 本地模拟账户实测）：
「全撤1(Z /)」「撤买1(X)」「撤卖(C)」——数量跟在按钮名后（无括号，
括号内为快捷键），数量为 0 时数字省略。

另含 query_full_orders 补 F5（落账复核主路径不能交旧数据）的键序断言。
"""
import pytest

import src.services.fast_trader as ft_module
from src.exceptions import ApiError, ErrorCode
from src.services.fast_trader import FastTrader


# ------------------------------------------------------------
# 测试替身
# ------------------------------------------------------------

class _FakeInfo:
    @property
    def rectangle(self):
        class _R:
            width = staticmethod(lambda: 100)
            height = staticmethod(lambda: 30)
        return _R()

    handle = None  # None → _alive 跳过 IsWindowVisible 判定


class FakeBtn:
    def __init__(self, text):
        self._text = text
        self.element_info = _FakeInfo()

    def window_text(self):
        return self._text


def _ft_with_buttons(monkeypatch, texts):
    """构造 FastTrader 并把撤单按钮塞进类级缓存（monkeypatch 自动恢复）"""
    ft = FastTrader()
    fields = {}
    for t, text in texts.items():
        if text is not None:
            fields[FastTrader._CANCEL[t]] = FakeBtn(text)
    monkeypatch.setattr(FastTrader, "_cache",
                        {"hwnd": 0x1234, "fields": fields, "grid": None,
                         "nav": None, "boxes": {}, "nav_items": {},
                         "bootstrapped": True})
    return ft


ROWS_2BUY = [{"操作": "买入", "合同编号": "1"}, {"操作": "买入", "合同编号": "2"}]
ROWS_1B1S = [{"操作": "买入", "合同编号": "1"}, {"操作": "卖出", "合同编号": "2"}]


# ------------------------------------------------------------
# 按钮计数解析
# ------------------------------------------------------------

class TestReadCancelButtonCounts:
    def test_real_text_forms(self, monkeypatch):
        """实测形态：数量跟按钮名后、0 时数字省略"""
        ft = _ft_with_buttons(monkeypatch, {
            "A": "全撤1(Z /)", "X": "撤买1(X)", "C": "撤卖(C)"})
        assert ft._read_cancel_button_counts() == {"A": 1, "X": 1, "C": 0}

    def test_zero_all_omitted_digits(self, monkeypatch):
        ft = _ft_with_buttons(monkeypatch, {
            "A": "全撤(Z /)", "X": "撤买(X)", "C": "撤卖(C)"})
        assert ft._read_cancel_button_counts() == {"A": 0, "X": 0, "C": 0}

    def test_missing_button_is_none(self, monkeypatch):
        ft = _ft_with_buttons(monkeypatch, {"A": "全撤2(Z)", "X": None,
                                            "C": "撤卖(C)"})
        counts = ft._read_cancel_button_counts()
        assert counts == {"A": 2, "X": None, "C": 0}

    def test_unrecognized_text_is_none(self, monkeypatch):
        """按钮改名（券商版本差异）→ None → 校验降级跳过"""
        ft = _ft_with_buttons(monkeypatch, {"A": "撤单(Z)", "X": "撤买(X)",
                                            "C": "撤卖(C)"})
        counts = ft._read_cancel_button_counts()
        assert counts["A"] is None


# ------------------------------------------------------------
# 交叉校验
# ------------------------------------------------------------

class TestVerifyCancelCounts:
    def test_consistent_passes(self, monkeypatch):
        ft = _ft_with_buttons(monkeypatch, {
            "A": "全撤2(Z)", "X": "撤买2(X)", "C": "撤卖(C)"})
        ft._verify_cancel_counts(ROWS_2BUY)  # 不抛即通过

    def test_total_mismatch_raises(self, monkeypatch):
        ft = _ft_with_buttons(monkeypatch, {
            "A": "全撤3(Z)", "X": "撤买2(X)", "C": "撤卖(C)"})
        with pytest.raises(ApiError) as ei:
            ft._verify_cancel_counts(ROWS_2BUY)
        assert ei.value.error_code == ErrorCode.DATA_MISMATCH
        assert "orders/full" in (ei.value.suggestion or "")

    def test_side_mismatch_raises_even_if_total_matches(self, monkeypatch):
        """总数对但买/卖错位（一买一卖换成一卖一买）同样拦截"""
        ft = _ft_with_buttons(monkeypatch, {
            "A": "全撤2(Z)", "X": "撤买(X)", "C": "撤卖2(C)"})
        with pytest.raises(ApiError) as ei:
            ft._verify_cancel_counts(ROWS_1B1S)  # 网格 买1/卖1，按钮 买0/卖2
        assert ei.value.error_code == ErrorCode.DATA_MISMATCH

    def test_unreadable_total_skips_check(self, monkeypatch):
        """全撤按钮不可读 → 降级跳过，不硬报错（锚点是增强非依赖）"""
        ft = _ft_with_buttons(monkeypatch, {"A": None, "X": None, "C": None})
        ft._verify_cancel_counts(ROWS_2BUY)  # 不抛即通过

    def test_side_unreadable_only_skips_side_check(self, monkeypatch):
        """总数可读、侧别不可读 → 总数校验仍生效，侧别降级"""
        ft = _ft_with_buttons(monkeypatch, {"A": "全撤1(Z)", "X": None,
                                            "C": None})
        with pytest.raises(ApiError):
            ft._verify_cancel_counts(ROWS_2BUY)  # 总数 1≠2
        ft2 = _ft_with_buttons(monkeypatch, {"A": "全撤2(Z)", "X": None,
                                             "C": None})
        ft2._verify_cancel_counts(ROWS_2BUY)  # 总数一致 → 通过


# ------------------------------------------------------------
# query_tab 接线与 query_full_orders 键序
# ------------------------------------------------------------

class _FakeWindow:
    def __init__(self, keys):
        self._keys = keys

    def set_focus(self):
        pass

    def type_keys(self, keys, pause=0):
        self._keys.append(keys)


class TestWiring:
    def _patch_query_chain(self, monkeypatch, keys, header, rows):
        monkeypatch.setattr(FastTrader, "_ensure_home",
                            lambda self: _FakeWindow(keys))
        monkeypatch.setattr(FastTrader, "_copy_grid",
                            lambda self, w: (header, rows))
        verified = []
        monkeypatch.setattr(FastTrader, "_verify_cancel_counts",
                            lambda self, rows: verified.append(len(rows)))
        return verified

    def test_orders_view_verifies_counts(self, monkeypatch):
        keys = []
        verified = self._patch_query_chain(
            monkeypatch, keys, ["委托时间"], [])
        FastTrader().query_tab("orders")
        assert verified == [0]              # orders 触发校验
        assert keys == ["{F8}", "{F5}"]     # 键序：切标签 → F5

    def test_positions_view_skips_verify(self, monkeypatch):
        keys = []
        verified = self._patch_query_chain(
            monkeypatch, keys, ["股票余额"], [])
        FastTrader().query_tab("positions")
        assert verified == []               # 持仓无按钮锚点，不校验
        assert keys == ["{F6}", "{F5}"]

    def test_full_orders_sends_f5_before_copy(self, monkeypatch):
        """落账复核主路径：F4 导航后必须 F5 拉新再复制（2026-10-15 定版）"""
        keys = []
        ft = FastTrader()
        monkeypatch.setattr(FastTrader, "_ensure_home",
                            lambda self: _FakeWindow(keys))
        monkeypatch.setattr(FastTrader, "_click_nav_item", lambda self, n: None)
        monkeypatch.setattr(FastTrader, "_window",
                            lambda self: _FakeWindow(keys))
        copied = []
        monkeypatch.setattr(FastTrader, "_copy_grid",
                            lambda self, w: copied.append(keys[:]) or ([], []))
        monkeypatch.setattr(FastTrader, "_back_home", lambda self: None)
        ft.query_full_orders()
        assert keys == ["{F5}"]             # 导航点击（无键）后仅 F5
        assert copied == [["{F5}"]]         # 复制前 F5 已发出
