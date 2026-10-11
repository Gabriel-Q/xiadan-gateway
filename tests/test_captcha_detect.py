# -*- coding: utf-8 -*-
"""验证码弹窗检测三级策略单元测试

_detect_captcha_full 的检测顺序：预测路径 → 顶层窗口快检 → 子对话框定向
扫描（2026-10-11 新增，~0.1s）→ 主窗口全树扫描（~0.8s 兜底）。
预测路径命中/子对话框命中均须记录路径、设置 _captcha_window。
"""
import types

from src.services.position_service import PositionService
from src.utils.uia import safe_control_type


class FakeEI:
    def __init__(self, control_type):
        self.control_type = control_type


class FakeEl:
    """最小控件假件：cid/class/类型/句柄/父子关系"""

    def __init__(self, cid=0, cls="Pane", ct="Pane", handle=1,
                 parent=None, children=None):
        self._cid = cid
        self._cls = cls
        self.handle = handle
        self._parent = parent
        self._children = children or []
        self.element_info = FakeEI(ct)
        for c in self._children:
            c._parent = self

    def control_id(self):
        return self._cid

    def class_name(self):
        return self._cls

    def parent(self):
        return self._parent

    def children(self):
        return self._children


def _make_service(main_window, find_impl):
    """绕过 __init__ 装配检测依赖；find_impl(window, cid) 模拟控件查找"""
    svc = object.__new__(PositionService)
    svc.logger = types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None)
    svc.window_service = types.SimpleNamespace(
        find_element_in_window=find_impl)
    svc._cached_window = main_window
    svc._captcha_window = None
    return svc


# 路径缓存是类级属性(跨实例共享),每个用例前重置防串扰
_reset_path_cache = None


def setup_function(_):
    global _reset_path_cache
    _reset_path_cache = PositionService._captcha_path_cache
    PositionService._captcha_path_cache = None
    PositionService._captcha_path_root_handle = None


def teardown_function(_):
    PositionService._captcha_path_cache = _reset_path_cache
    PositionService._captcha_path_root_handle = None


def _tree():
    """主窗口 → [无关对话框, 验证码对话框(面板→图片2405, 输入框2404)]"""
    image = FakeEl(cid=2405, cls="Img", ct="Image", handle=31)
    input_ = FakeEl(cid=2404, cls="Edit", ct="Edit", handle=32)
    pane = FakeEl(children=[image, input_], handle=30)
    dialog = FakeEl(cls="#32770", ct="Dialog", handle=3,
                    children=[pane])
    other = FakeEl(cls="Static", ct="Static", handle=2)
    main = FakeEl(cls="Txunjia_main", ct="Window", handle=1,
                  children=[other, dialog])
    return main, dialog, image


def test_full_detect_via_child_dialog_scan(monkeypatch):
    """无预测路径时:子对话框定向扫描命中(免 ~0.8s 全树遍历)"""
    main, dialog, image = _tree()
    # find 仅对「以对话框为根」的查找生效——模拟定向扫描路径
    svc = _make_service(main, lambda w, cid, descendants=None: (
        image if w is dialog and cid == 2405 else None))
    monkeypatch.setattr(svc, "_detect_captcha", lambda w: False)
    assert svc._detect_captcha_full(main) is True
    assert svc._captcha_window is dialog
    assert svc._captcha_path_cache  # 命中后记录路径供下次预测


def test_full_detect_predicted_path_skips_all_scans(monkeypatch):
    """预测路径命中:不触顶层快检/子对话框/全树扫描(任一被调即失败)"""
    main, dialog, image = _tree()
    svc = _make_service(main, lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("预测路径命中时不应发生任何树扫描")))
    svc._record_captcha_path(image)  # 生产同款:命中后记录路径
    assert svc._captcha_path_cache
    assert svc._detect_captcha_full(main) is True
    assert svc._captcha_window is dialog


def test_full_detect_full_tree_fallback(monkeypatch):
    """预测/顶层/子对话框全脱靶:回退主窗口全树扫描并记录路径"""
    main, dialog, image = _tree()
    calls = []

    def find(w, cid, descendants=None):
        calls.append(w)
        # 只有「以主窗口为根」的全树查找才能找到(模拟弹窗非直接子级变体)
        return image if w is main and cid == 2405 else None

    svc = _make_service(main, find)
    monkeypatch.setattr(svc, "_detect_captcha", lambda w: False)
    # 子对话框扫描只找 2405,把图片挂在主窗口直接子级 Static 下模拟变体
    image._parent._children.remove(image)
    main._children.append(image)
    assert svc._detect_captcha_full(main) is True
    assert len(calls) >= 1 and any(w is main for w in calls)
    assert svc._captcha_path_cache


def test_full_detect_miss_returns_false(monkeypatch):
    """无弹窗:四级全脱靶返回 False,不设置 _captcha_window"""
    main, _, _ = _tree()
    svc = _make_service(main, lambda w, cid, descendants=None: None)
    monkeypatch.setattr(svc, "_detect_captcha", lambda w: False)
    assert svc._detect_captcha_full(main) is False
    assert svc._captcha_window is None
    assert not svc._captcha_path_cache


def test_cached_detect_sets_dialog_window():
    """_detect_captcha_cached:路径命中时 _captcha_window 指向对话框"""
    main, dialog, image = _tree()
    svc = _make_service(main, lambda *a, **k: None)
    svc._record_captcha_path(image)
    assert svc._detect_captcha_cached(main) is True
    assert svc._captcha_window is dialog


def test_cached_detect_stale_root_handle_rejected():
    """主窗口句柄已变(重建):预测路径按失效处理返回 False"""
    main, _, _ = _tree()
    svc = _make_service(main, lambda *a, **k: None)
    svc._captcha_path_cache = [("Dialog", "#32770", 3)]
    svc._captcha_path_root_handle = 999  # 与 main.handle=1 不符
    assert svc._detect_captcha_cached(main) is False


def test_resolve_path_backtracks_over_identical_siblings():
    """同规格兄弟歧义:首分支走错时带回溯在后续分支命中(2026-10-11 实测回归)"""
    main, dialog, image = _tree()
    # 在验证码对话框之前插入同 (类名,cid) 的干扰对话框(其下无 2405)
    decoy_pane = FakeEl(cls="Pane", ct="Pane", handle=40)
    decoy = FakeEl(cls="#32770", ct="Dialog", handle=41,
                   children=[decoy_pane])
    main._children.insert(0, decoy)
    svc = _make_service(main, lambda *a, **k: None)
    svc._record_captcha_path(image)
    resolved = svc._resolve_captcha_image_via_path(main)
    assert resolved is image  # 首匹配会进 decoy,回溯才能命中真弹窗


def test_record_path_roundtrip():
    """命中后记录的路径可被 _resolve_captcha_image_via_path 原样解析"""
    main, dialog, image = _tree()
    svc = _make_service(main, lambda *a, **k: None)
    svc._record_captcha_path(image)
    assert svc._captcha_path_cache  # 记录成功
    resolved = svc._resolve_captcha_image_via_path(main)
    assert resolved is image
