# -*- coding: utf-8 -*-
"""传统端点极速映射测试：order.fast_mode 两态 × 查询/撤单端点矩阵

2026-10-12 用户定版：fast_mode=true 时传统查询端点（/positions、
/account/balance、/trades/today、/orders/pending、/orders/full）与撤单
A/X/C 在路由层映射到极速单页流；false 时传统路径一行不改（严格隔离）；
L（撤最后）极速不支持，显式 VALIDATION_ERROR。

不碰真实 UI：TaskQueue.submit 类级 mock 捕获 task_name（断言走极速还是
传统任务），FastTrader/PositionService 方法不动；fast off 分支的
ensure_connected_or_discard（UI 状态格读取）一并 mock。
"""
import json

import pytest

import src.models.config as config_module
from src.api.helpers import strip_blank_columns
from src.api.task_queue import TaskQueue


@pytest.fixture
def make_client(monkeypatch, tmp_path):
    """工厂 fixture：make_client(fast_mode) → (test_client, submit 捕获字典)"""

    def _make(fast_mode: bool):
        cfg = {
            "trading_app_paths": ["C:\\xiadan.exe"],
            "host": "127.0.0.1",
            "port": 5000,
            "order": {"fast_mode": fast_mode},
        }
        p = tmp_path / f"app_config_{fast_mode}.json"
        p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(config_module, "CONFIG_PATH", str(p))
        config_module.AppConfig._reset_instance()

        captured = {}

        def fake_submit(self, func, task_name, params=None, timeout=None, **kw):
            captured["task_name"] = task_name
            captured["timeout"] = timeout
            # 真正执行 func：路由 lambda 内构造的 FastTrader 方法已被类级
            # monkeypatch 替换，构造本身无 UI 副作用（logger+config+壳）
            return func()

        monkeypatch.setattr(TaskQueue, "submit", fake_submit)
        # fast off 分支的断连复查（真实实现读状态格 UI）
        import src.services.broker_link as bl
        monkeypatch.setattr(bl, "ensure_connected_or_discard",
                            lambda *a, **k: None)

        from src.api.routes import create_app
        return create_app().test_client(), captured

    return _make


SENTINEL_ROWS = [{"": "", "证券代码": "000001", "证券名称": "平安银行"}]


# ── fast_mode=true：传统端点映射极速任务 ─────────────────────

class TestFastOnMapping:
    def test_positions_maps_to_f6(self, make_client, monkeypatch):
        client, cap = make_client(True)
        import src.services.fast_trader as ft
        monkeypatch.setattr(ft.FastTrader, "query_tab",
                            lambda self, view: [dict(r) for r in SENTINEL_ROWS])
        r = client.get("/positions")
        body = r.get_json()
        assert body["status"] == "success"
        assert isinstance(body["data"], list)          # 传统契约=裸列表
        assert "" not in body["data"][0]               # 空占位列已丢弃
        assert body["data"][0]["证券代码"] == "000001"
        assert cap["task_name"] == "fast_query_positions"
        assert cap["timeout"] == 20

    def test_balance_maps_to_fast(self, make_client, monkeypatch):
        client, cap = make_client(True)
        import src.services.fast_trader as ft
        monkeypatch.setattr(ft.FastTrader, "query_balance",
                            lambda self: {"可用金额": "34403.26"})
        r = client.get("/account/balance")
        body = r.get_json()
        assert body["data"] == {"可用金额": "34403.26"}  # 平铺字典，契约一致
        assert cap["task_name"] == "fast_balance"

    def test_trades_maps_to_f7(self, make_client, monkeypatch):
        client, cap = make_client(True)
        import src.services.fast_trader as ft
        monkeypatch.setattr(ft.FastTrader, "query_tab", lambda self, view: [])
        r = client.get("/trades/today")
        assert r.get_json()["data"] == []
        assert cap["task_name"] == "fast_query_trades"

    def test_orders_pending_maps_to_f8_cancellable(self, make_client, monkeypatch):
        client, cap = make_client(True)
        import src.services.fast_trader as ft
        monkeypatch.setattr(ft.FastTrader, "query_tab",
                            lambda self, view: SENTINEL_ROWS)
        r = client.get("/orders/pending")
        assert cap["task_name"] == "fast_query_orders"   # F8 可撤（在途语义）
        assert "" not in r.get_json()["data"][0]

    def test_orders_full_maps_to_f4_full(self, make_client, monkeypatch):
        client, cap = make_client(True)
        import src.services.fast_trader as ft
        monkeypatch.setattr(ft.FastTrader, "query_full_orders",
                            lambda self: SENTINEL_ROWS)
        r = client.get("/orders/full")
        assert r.get_json()["status"] == "success"
        assert cap["task_name"] == "fast_full_orders"
        assert cap["timeout"] == 25

    def test_cancel_maps_a(self, make_client, monkeypatch):
        client, cap = make_client(True)
        import src.services.fast_trader as ft
        monkeypatch.setattr(ft.FastTrader, "cancel",
                            lambda self, t: {"cancel_type": t, "clicked": True})
        r = client.post("/orders/cancel-all", json={"type": "A"})
        assert r.get_json()["status"] == "success"
        assert cap["task_name"] == "fast_cancel"

    def test_cancel_fast_on_L_rejected(self, make_client):
        """fast on + type=L：显式 VALIDATION_ERROR，任务不入队（用户定版）"""
        client, cap = make_client(True)
        r = client.post("/orders/cancel-all", json={"type": "L"})
        body = r.get_json()
        assert body["error_code"] == "VALIDATION_ERROR"
        assert "撤最后" in body["message"]
        assert "task_name" not in cap                   # 未入队


# ── fast_mode=false：传统路径一行不改 ────────────────────────

class TestFastOffTraditional:
    def test_positions_traditional(self, make_client, monkeypatch):
        client, cap = make_client(False)
        import src.services.position_service as ps
        monkeypatch.setattr(ps.PositionService, "get_position",
                            lambda self: [dict(r) for r in SENTINEL_ROWS])
        r = client.get("/positions")
        body = r.get_json()
        assert body["data"][0]["证券代码"] == "000001"
        assert cap["task_name"] == "get_position"       # 传统任务名

    def test_orders_pending_traditional_returns_full(self, make_client, monkeypatch):
        """fast off 时 /orders/pending 保持历史行为（当日全量，用户定版）"""
        client, cap = make_client(False)
        import src.services.position_service as ps
        monkeypatch.setattr(ps.PositionService, "get_today_orders",
                            lambda self, refresh_until_match=None: [])
        r = client.get("/orders/pending")
        assert r.get_json()["status"] == "success"
        assert cap["task_name"] == "get_today_orders"

    def test_orders_full_traditional(self, make_client, monkeypatch):
        client, cap = make_client(False)
        import src.services.position_service as ps
        monkeypatch.setattr(ps.PositionService, "get_today_orders",
                            lambda self, refresh_until_match=None: [])
        r = client.get("/orders/full")
        assert r.get_json()["status"] == "success"
        assert cap["task_name"] == "get_today_orders"   # 与 pending 传统实现同源

    def test_cancel_L_traditional_allowed(self, make_client, monkeypatch):
        """fast off 时 L（撤最后）照常走传统流程"""
        client, cap = make_client(False)
        import src.services.trading_service as ts
        monkeypatch.setattr(ts.TradingService, "cancel_all_orders",
                            lambda self, t: {"cancelled": t})
        r = client.post("/orders/cancel-all", json={"type": "L"})
        assert r.get_json()["status"] == "success"
        assert cap["task_name"] == "cancel_all_orders"


# ── 极速模式市价单拒绝（2026-10-12 用户定版：不回落传统，避免切页）──

MARKET_MSG_FRAGMENT = "极速模式下不支持市价单"


class TestMarketOrderPolicy:
    def test_orders_market_rejected_in_fast_mode(self, make_client, monkeypatch):
        """POST /orders fast on + market → VALIDATION_ERROR，任务不入队、
        幂等记录不消费（校验位于幂等检查之前）"""
        client, cap = make_client(True)
        from src.api.idempotency import IdempotencyChecker
        idem_calls = []
        monkeypatch.setattr(IdempotencyChecker, "check_and_record",
                            lambda self, key: idem_calls.append(key))
        r = client.post("/orders", json={
            "code": "601991", "status": "1", "amount": "100",
            "price_type": "market"}, headers={"Idempotency-Key": "k-market-1"})
        body = r.get_json()
        assert body["error_code"] == "VALIDATION_ERROR"
        assert MARKET_MSG_FRAGMENT in body["message"]
        assert "涨停价" in body["message"]          # 引导限价挂涨跌停模拟
        assert "task_name" not in cap               # 未入队
        assert idem_calls == []                     # 幂等键未被消费

    def test_fast_orders_market_rejected(self, make_client):
        """/fast/orders 市价 → 同样拒绝（原回落分支已删除）"""
        client, cap = make_client(True)
        r = client.post("/fast/orders", json={
            "code": "000001", "status": "1", "amount": "100",
            "price_type": "market"})
        body = r.get_json()
        assert body["error_code"] == "VALIDATION_ERROR"
        assert MARKET_MSG_FRAGMENT in body["message"]
        assert "task_name" not in cap

    def test_fast_orders_limit_still_works(self, make_client, monkeypatch):
        """校验收紧不误伤限价路径"""
        client, cap = make_client(True)
        import src.services.fast_trader as ft
        monkeypatch.setattr(ft.FastTrader, "place_order",
                            lambda self, **kw: {"submitted": True, **kw})
        r = client.post("/fast/orders", json={
            "code": "000001", "status": "1", "amount": "100",
            "price": "10.60", "price_type": "limit"})
        body = r.get_json()
        assert body["status"] == "success"
        assert body["data"]["submitted"] is True
        assert cap["task_name"] == "fast_place_order"

    def test_orders_market_traditional_when_fast_off(self, make_client, monkeypatch):
        """fast off 时市价单照常走传统 Trader（行为不变）"""
        client, cap = make_client(False)
        import src.core.trader as trader_mod
        monkeypatch.setattr(trader_mod.Trader, "place_order",
                            lambda self, **kw: {"confirmed": True})
        r = client.post("/orders", json={
            "code": "601991", "status": "1", "amount": "100",
            "price_type": "market"}, headers={"Idempotency-Key": "k-market-2"})
        body = r.get_json()
        assert body["status"] == "success"
        assert cap["task_name"] == "place_order"


# ── 纯单元：空占位列丢弃 ─────────────────────────────────────

class TestStripBlankColumns:
    def test_drops_blank_key_only(self):
        rows = [{"": "", "  ": " ", "证券代码": "000001"}]
        out = strip_blank_columns(rows)
        assert list(out[0].keys()) == ["证券代码"]

    def test_keeps_named_columns(self):
        rows = [{"证券代码": "000001", "备注": "全部撤单"}]
        assert strip_blank_columns(rows) == rows

    def test_empty_rows(self):
        assert strip_blank_columns([]) == []
