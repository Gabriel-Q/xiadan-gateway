# -*- coding: utf-8 -*-
"""业务并发准入闸门测试：容量封顶 / 管理端点绕行 / teardown 释放 / 配置校验

背景（2026-10-12 过载压测实锤）：waitress 线程固定，任务风暴下业务请求
线程全部停泊在队列同步等待上，管理端点饿死 15s+。闸门以业务并发上限
把物理线程池一分为二（见 src/api/admission_gate 模块 docstring）。

不碰真实 UI：TaskQueue.submit 类级 mock（闸门在队列外层，纯计数器），
worker 不执行任何任务体；AppConfig 经 CONFIG_PATH 重定向到临时文件
（test_core.py 同款模式）。
"""
import json

import pytest

import src.models.config as config_module
from src.api.admission_gate import BusinessGate


# ── 纯单元：BusinessGate 计数器 ─────────────────────────────

class TestBusinessGate:
    def test_acquire_release_roundtrip(self):
        g = BusinessGate(2)
        assert g.active == 0
        assert g.try_acquire() is True
        assert g.active == 1
        g.release()
        assert g.active == 0

    def test_cap_enforced(self):
        g = BusinessGate(2)
        assert g.try_acquire() and g.try_acquire()
        # 满员：第 3 个立即拒绝（毫秒级，不停泊）
        assert g.try_acquire() is False
        assert g.active == 2

    def test_release_allows_reacquire(self):
        g = BusinessGate(1)
        assert g.try_acquire()
        assert g.try_acquire() is False
        g.release()
        assert g.try_acquire() is True

    def test_release_without_acquire_safe(self):
        """release 对未 acquire 的调用是安全空操作（teardown 防御）"""
        g = BusinessGate(1)
        g.release()
        g.release()
        assert g.active == 0

    def test_invalid_max_raises(self):
        for bad in (0, -1, "4", True, 1.5, None):
            with pytest.raises(ValueError):
                BusinessGate(bad)


# ── 集成：create_app 接线（CONFIG_PATH 重定向 + submit mock）────

@pytest.fixture
def client(monkeypatch, tmp_path):
    cfg = {
        "trading_app_paths": ["C:\\xiadan.exe"],
        "host": "127.0.0.1",
        "port": 5000,
        # 业务并发上限=1：泄漏即第二单 QUEUE_FULL，释放逻辑可观测
        "api": {"server_threads": 16, "business_max_concurrent": 1},
    }
    p = tmp_path / "app_config.json"
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(p))
    config_module.AppConfig._reset_instance()

    from src.api.task_queue import TaskQueue
    monkeypatch.setattr(TaskQueue, "submit",
                        lambda *a, **k: {"rows": [], "count": 0})

    from src.api.routes import create_app
    return create_app().test_client()


class TestGateWiring:
    def test_business_request_succeeds_and_releases(self, client):
        """business_max_concurrent=1：连续两个业务请求都成功——
        teardown 必须释放名额，否则第二单即 QUEUE_FULL（零泄漏证明）"""
        for _ in range(2):
            r = client.get("/positions")
            assert r.status_code == 200
            assert r.get_json()["status"] == "success"

    def test_gate_full_rejects_business(self, monkeypatch, client):
        """闸门满员 → 业务请求毫秒级 QUEUE_FULL（语义与队列满一致）"""
        monkeypatch.setattr(BusinessGate, "try_acquire", lambda self: False)
        r = client.get("/positions")
        body = r.get_json()
        assert body["status"] == "error"
        assert body["error_code"] == "QUEUE_FULL"
        assert "业务并发已达上限" in body["message"]

    def test_management_bypass_when_gate_full(self, monkeypatch, client):
        """闸门满员时管理端点照常响应（容量预留的核心保证）"""
        monkeypatch.setattr(BusinessGate, "try_acquire", lambda self: False)
        for path in ("/health", "/queue/status"):
            r = client.get(path)
            assert r.status_code == 200
            assert r.get_json()["status"] == "success"


# ── 配置校验：api 段 ────────────────────────────────────────

class TestApiConfigValidation:
    def _make_config(self, monkeypatch, tmp_path, api_cfg):
        cfg = {
            "trading_app_paths": ["C:\\xiadan.exe"],
            "host": "127.0.0.1",
            "port": 5000,
            "api": api_cfg,
        }
        p = tmp_path / "app_config.json"
        p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(config_module, "CONFIG_PATH", str(p))
        config_module.AppConfig._reset_instance()
        return config_module.AppConfig()

    def test_valid_api_config_passes(self, monkeypatch, tmp_path):
        c = self._make_config(monkeypatch, tmp_path,
                              {"server_threads": 16, "business_max_concurrent": 12})
        assert c.validate() == []

    def test_defaults_when_absent(self, monkeypatch, tmp_path):
        """缺省 api 段 → 代码内置默认（16 线程 / 业务并发 12）"""
        c = self._make_config(monkeypatch, tmp_path, None)
        assert c.validate() == []
        assert c.get_api_config()["server_threads"] == 16
        assert c.get_api_config()["business_max_concurrent"] == 12

    def test_invalid_threads_rejected(self, monkeypatch, tmp_path):
        c = self._make_config(monkeypatch, tmp_path, {"server_threads": 1})
        assert any("api.server_threads" in e for e in c.validate())

    def test_invalid_business_rejected(self, monkeypatch, tmp_path):
        c = self._make_config(monkeypatch, tmp_path,
                              {"server_threads": 16, "business_max_concurrent": 0})
        assert any("api.business_max_concurrent" in e for e in c.validate())

    def test_no_reserve_for_management_rejected(self, monkeypatch, tmp_path):
        """业务并发吃满线程池（不留 ≥2 线程）→ 拒绝（管理端点会饿死）"""
        c = self._make_config(monkeypatch, tmp_path,
                              {"server_threads": 4, "business_max_concurrent": 4})
        errors = c.validate()
        assert any("api.business_max_concurrent" in e for e in errors)
        assert any("管理端点" in e for e in errors)
