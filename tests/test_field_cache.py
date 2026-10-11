# -*- coding: utf-8 -*-
"""F1/F2 字段引用缓存单元测试

_field_cache_lookup/store/invalidate：按（根句柄，页别）缓存九个固定控件
引用与 UI 文本基线，下单免主遍历；存活探针（rectangle 可读）拦截失效元素；
联动失败作废后可重录。
"""
import types

import pytest

from src.core.trader import Trader
from src.constants import (
    CONTROL_ID_CODE, CONTROL_ID_STOCK_NAME, CONTROL_ID_AMOUNT,
    CONTROL_ID_PRICE, CONTROL_ID_SUBMIT, CONTROL_ID_PRICE_TYPE,
    CONTROL_ID_BUYABLE_LABEL, CONTROL_ID_BUYABLE, CONTROL_ID_SELLABLE,
)


class FakeEI:
    def __init__(self, control_type):
        self.control_type = control_type


class FakeField:
    def __init__(self, cid, dead=False):
        self._cid = cid
        self._dead = dead

    def control_id(self):
        return self._cid

    def window_text(self):
        return ""

    def rectangle(self):
        if self._dead:
            raise RuntimeError("stale element")
        return types.SimpleNamespace(left=0, top=0, right=10, bottom=10)


def _make_service():
    svc = object.__new__(Trader)
    svc.logger = types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None)
    svc._ui_text_baseline = None
    return svc


def _fields(dead_cids=()):
    return {
        cid: FakeField(cid, dead=(cid in dead_cids))
        for cid in (CONTROL_ID_CODE, CONTROL_ID_STOCK_NAME, CONTROL_ID_AMOUNT,
                    CONTROL_ID_PRICE, CONTROL_ID_SUBMIT, CONTROL_ID_PRICE_TYPE,
                    CONTROL_ID_BUYABLE_LABEL, CONTROL_ID_BUYABLE)
    }


def setup_function(_):
    Trader._field_cache = None
    Trader._ui_baseline_cache = None


def teardown_function(_):
    Trader._field_cache = None
    Trader._ui_baseline_cache = None


def test_store_then_lookup_hit_same_page():
    main = types.SimpleNamespace(handle=1)
    svc = _make_service()
    fields = _fields()
    svc._ui_text_baseline = {"a", "b"}
    svc._field_cache_store(main, "F1", list(fields.values()))
    hit = svc._field_cache_lookup(main, "F1")
    assert hit is not None and CONTROL_ID_CODE in hit
    # 其他页别/其他句柄不命中
    assert svc._field_cache_lookup(main, "F2") is None
    assert svc._field_cache_lookup(types.SimpleNamespace(handle=99), "F1") is None


def test_store_rejects_sparse_fields():
    main = types.SimpleNamespace(handle=1)
    svc = _make_service()
    sparse = [FakeField(cid) for cid in (CONTROL_ID_CODE,
                                         CONTROL_ID_STOCK_NAME,
                                         CONTROL_ID_AMOUNT)]
    svc._ui_text_baseline = {"a"}
    svc._field_cache_store(main, "F1", sparse)
    assert Trader._field_cache is None  # <5 个字段疑似停错页,不缓存


def test_lookup_liveness_probe_invalidates_dead_field():
    main = types.SimpleNamespace(handle=1)
    svc = _make_service()
    fields = _fields(dead_cids=(CONTROL_ID_AMOUNT,))  # 一个字段已死
    svc._ui_text_baseline = {"a"}
    svc._field_cache_store(main, "F1", list(fields.values()))
    assert Trader._field_cache is not None
    assert svc._field_cache_lookup(main, "F1") is None  # 探针拦截
    assert Trader._field_cache.get((1, "F1")) is None   # 且已作废


def test_invalidate_scopes_to_root_handle():
    main1 = types.SimpleNamespace(handle=1)
    main2 = types.SimpleNamespace(handle=2)
    svc = _make_service()
    fields = _fields()
    svc._ui_text_baseline = {"a"}
    svc._field_cache_store(main1, "F1", list(fields.values()))
    svc._field_cache_store(main2, "F1", list(fields.values()))
    svc._field_cache_invalidate(main1)
    assert svc._field_cache_lookup(main1, "F1") is None
    assert svc._field_cache_lookup(main2, "F1") is not None  # 另一窗口不受影响
