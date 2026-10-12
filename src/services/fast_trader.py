"""极速模式：双向委托单页流（2026-10-12 全新设计，最小路径优先）

设计定版（用户，2026-10-12 二次修订）：
- 界面只在「双向委托」页与 F4 查询页之间跳转；下单/撤单/页内查询全在双向委托页
- 启动后默认导航到双向委托页，三个勾选框强制到目标状态：
  3551 只显示可撤委托 = **勾选**（F8 列表只看可撤委托，撤掉即消失）；
  3552 买卖代码联动 = 未勾（买卖两侧代码独立）；
  3553 委托后不清空 = 未勾（每次下单后表单自动清空，无双击清不净的残留）
- 全量委托（含已撤）不在 F8 查——一律走 F4「当日委托」页（/fast/orders/full）
- 文本输入：双击输入框 → 一次 Backspace → 键入文本，全程零延迟
- 提交前唯一强门控：可买(1018)/可卖(1019) 数值为 ≥0 的数字，否则报错；
  其余下单防御（名称联动校验/弹窗扫描/模式校验等）一律不做
- 页内查询（F6 持仓/F7 成交/F8 可撤委托）：Ctrl+C 前单击网格正中聚焦
- 验证码：鼠标点一次输入框 → 键入 OCR 数字，零延迟
- 市价单：路由层直接回落传统 Trader 流程，极速模式只做限价单

借鉴的历史经验（仅必要件）：
- TaskQueue 单 worker 串行所有 UI 操作（与旧端点互斥，架构铁律）
- 类级控件缓存 + 存活探针（字段引用跨请求复用，失效回退全遍历重建）
- keybd_event 双发 Ctrl+C（中文输入法吞首发）
- 验证码弹窗控件 cid：2405 图 / 2404 输入框 / 1 确定 / 2 取消
- 资金余额 = 资金股票页 control_id 批量读取（BALANCE_FIELDS，无验证码）

红线（用户定版 2026-10-12）：
- 本模块严禁发送 F1/F2——焦点若不在双向委托页右侧，F1/F2 会全局跳转
  到专门的买入/卖出页。方向切换只靠双向委托页买卖双表单（cid 区分），
  页面导航只靠导航树点击；功能键（F6/F7/F8）发送前必须先单击网格正中
  把焦点收进右侧，杜绝全局跳页。
- 双向委托页仅支持限价单；市价单由路由层回落传统 Trader 流程。
"""
import os
import re
import time

import win32gui

from src.constants import BALANCE_FIELDS
from src.exceptions import ApiError, ErrorCode
from src.models.config import AppConfig
from src.services.window_service import WindowService
from src.utils.logger import Logger
from src.utils.poll import timed


class FastTrader:
    """双向委托页极速交易器（方法在 TaskQueue worker 线程内执行）"""

    # 买卖双表单对称 cid（买侧与 F1/F2 页完全一致——实测 2026-10-12）
    _FORMS = {
        "1": {"code": 1032, "price": 1033, "amount": 1034,
              "submit": 1006, "ready": 1018},
        "2": {"code": 1035, "price": 1058, "amount": 1039,
              "submit": 1008, "ready": 1019},
    }
    _CHECKBOXES = (3551, 3552, 3553)      # 只显示可撤委托/买卖代码联动/委托后不清空
    # 勾选框目标状态（用户定版 2026-10-12 二次修订）：
    # 3551 只显示可撤委托=勾选（F8 只看可撤）；3552 联动/3553 不清空=未勾
    _BOX_TARGETS = {3551: True, 3552: False, 3553: False}
    _CANCEL = {"A": 30001, "X": 30002, "C": 30003}
    _GRID_CID = 1047                       # CVirtualGridCtrl（页内三标签共用）
    _NAV_NAME = "双向委托"
    _F_KEYS = {"positions": "{F6}", "trades": "{F7}", "orders": "{F8}"}
    _READY_RE = re.compile(r"^\d+(\.\d+)?$")
    _CAPTCHA_IMAGE = 2405
    _CAPTCHA_INPUT = 2404
    _CAPTCHA_OK = 1
    _CAPTCHA_CANCEL = 2

    # 类级缓存（hwnd 归属 + UIA 引用；同页失效探针 + 全遍历重建）
    _cache = {"hwnd": None, "fields": {}, "grid": None, "nav": None,
              "boxes": {}, "nav_items": {}, "bootstrapped": False}

    def __init__(self, ocr_service=None):
        self.logger = Logger.get_instance()
        self.config = AppConfig()
        self.ws = WindowService()
        self._ocr = ocr_service

    # ------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------

    @staticmethod
    def _alive(el) -> bool:
        """控件存活且可见。可见性必须查——页面切换是隐藏而非销毁（2026-10-12
        实锤：市价单回落传统路径切到 F1 后，双向委托页控件被隐藏，缓存引用
        的 rect 依旧有效，仅查矩形会误判「已在主页」，极速任务全跑在 F1 页上）
        """
        try:
            info = el.element_info
            r = info.rectangle
            if r.width() <= 0 or r.height() <= 0:
                return False
            h = info.handle
            if h and not win32gui.IsWindowVisible(h):
                return False
            return True
        except Exception:
            return False

    def _window(self):
        window = self.ws.get_trading_window()
        if window is None:
            raise ApiError(ErrorCode.WINDOW_NOT_FOUND,
                           "未找到交易窗口 '网上股票交易系统5.0'",
                           suggestion="请确认券商程序已启动且已登录")
        return window

    def _rebuild(self, window) -> None:
        """全遍历一次，重建全部控件缓存（字段/网格/导航/勾选框）"""
        fields, boxes, nav, grid = {}, {}, None, None
        for el in window.descendants():
            try:
                info = el.element_info
                if info.rectangle.width() <= 0:
                    continue
                h = info.handle
                if h and not win32gui.IsWindowVisible(h):
                    continue  # 隐藏控件不收（页面切换=隐藏而非销毁）
                cid = info.control_id
                name = info.name
                if cid in (1032, 1033, 1034, 1006, 1018,
                           1035, 1058, 1039, 1008, 1019,
                           30001, 30002, 30003):
                    fields[cid] = el
                elif cid in self._CHECKBOXES:
                    boxes[cid] = el
                elif cid == self._GRID_CID and grid is None:
                    grid = el
                if name == self._NAV_NAME and nav is None:
                    nav = el
            except Exception:
                continue
        self._cache.update(hwnd=window.handle, fields=fields, grid=grid,
                           nav=nav, boxes=boxes)
        if len(fields) < 13:
            self.logger.warning(f"极速模式控件收集不全: fields={len(fields)} "
                                f"（可能未在双向委托页）")

    def _ensure_home(self, force: bool = False):
        """确保停在双向委托页；返回主窗口引用。

        热探针 = 3552「买卖代码联动」钮存活——它是双向委托导航页独有控件。
        不可用 1035（卖侧代码框）判断：F1 页的「双向」布局下同样有卖侧表单
        （2026-10-12 实锤：市价单回落传统路径后页面停在 F1，极速任务全部
        在 F1 页上跑，F8 拿到 F1 页全量委托 207 行）。
        失效/force 时 set_focus 后点导航树（缓存引用优先）回页。

        红线：严禁走 WindowService.activate_window——其内置 ESC×5 会把
        客户端全局打回 F1 买入页（2026-10-12 实锤事故：键入落进 F1 页
        默认聚焦的代码框）。极速模式的一切页面导航只允许导航树点击。
        导航点击后必须重建并验证控件在位才算落页——点击可能被遮挡
        窗口（如左侧终端压住导航树区域）静默吸收。
        """
        window = self._window()
        c = self._cache
        if not force and c["hwnd"] == window.handle and self._alive(c["boxes"].get(3552)):
            return window

        last_fields = 0
        for attempt in (1, 2):
            window.set_focus()
            nav = c["nav"] if self._alive(c["nav"]) else None
            if nav is None:
                for el in window.descendants():
                    try:
                        if el.element_info.name == self._NAV_NAME:
                            nav = el
                            break
                    except Exception:
                        continue
            if nav is None:
                raise ApiError(ErrorCode.CONTROL_NOT_FOUND,
                               "导航树中未找到「双向委托」入口",
                               suggestion="请人工点一次双向委托页后重试")
            nav.click_input()
            time.sleep(0.35)  # 页面切换渲染窗口（导航动作本身，非文本输入）
            self._rebuild(self._window())
            if (len(c["fields"]) >= 13 and self._alive(c["fields"].get(1035))
                    and self._alive(c["boxes"].get(3552))):
                return self._window()
            last_fields = len(c["fields"])
            self.logger.warning(f"双向委托导航第 {attempt} 次未落页"
                                f"（fields={last_fields}/13），重试")
            window = self._window()
        raise ApiError(ErrorCode.CONTROL_NOT_FOUND,
                       f"双向委托页导航失败（两次导航均未落页，"
                       f"最后.fields={last_fields}/13——客户端可能停在"
                       f"特殊页面或有窗口遮挡导航树）",
                       suggestion="请人工点一次双向委托页后重试")

    # ------------------------------------------------------------
    # 启动引导：回主页 + 三勾选框确保未勾选
    # ------------------------------------------------------------

    @staticmethod
    def _box_checked(el) -> bool:
        """owner-drawn 勾选框状态：左侧方块暗像素 ≥10 = 勾选（标定 27/0）"""
        try:
            im = el.capture_as_image().convert("RGB")
            box = im.crop((0, 0, 20, min(22, im.height)))
            dark = sum(1 for p in list(box.getdata())
                       if p[0] < 100 and p[1] < 100 and p[2] < 100)
            return dark >= 10
        except Exception:
            return False

    def _enforce_checkboxes(self) -> dict:
        """把可见的勾选框强制到目标状态（_BOX_TARGETS）

        仅在网关启动引导（bootstrap）时执行——勾选框状态在客户端会话内
        持久有效（用户确认 2026-10-12），无需每次导航后复查。3551 只在
        委托标签可见——缺席即跳过。
        """
        result = {}
        for cid in self._CHECKBOXES:
            target = self._BOX_TARGETS[cid]
            el = self._cache["boxes"].get(cid)
            if el is None or not self._alive(el):
                continue
            state = self._box_checked(el)
            if state != target:
                el.click_input()
                time.sleep(0.2)
                state2 = self._box_checked(el)
                result[cid] = "fixed" if state2 == target else "click-failed"
            else:
                result[cid] = "ok"
        return result

    def bootstrap(self) -> dict:
        """导航到双向委托页 → F8 → 三勾选框强制到目标状态（_BOX_TARGETS）

        3551 只显示可撤委托=勾选（F8 只看可撤）；3552 联动 / 3553 不清空=未勾。
        """
        window = self._ensure_home(force=True)
        window.set_focus()
        # F8 必须在网格失焦时发送——网格持焦会吞掉功能键（CVirtualGridCtrl
        # 消费按键，页级加速键不触发，2026-10-12 实锤：先点网格中心再按
        # F8，3551 永不渲染）。F8 不跳页，无 F1/F2 红线问题。
        window.type_keys("{F8}", pause=0.05)
        # 3551 随委托标签工具条渲染，实测可晚于 1s——轮询到位再读状态
        for _ in range(4):
            time.sleep(0.4)
            self._rebuild(self._window())
            if 3551 in self._cache["boxes"]:
                break
        res = self._enforce_checkboxes()
        result = {cid: res.get(cid, "missing") for cid in self._CHECKBOXES}
        self._cache["bootstrapped"] = True
        self.logger.info(f"极速模式引导完成: 3551/3552/3553 = "
                         f"{result[3551]}/{result[3552]}/{result[3553]}")
        return {"checkboxes": {str(k): v for k, v in result.items()},
                "page": "双向委托"}

    # ------------------------------------------------------------
    # 下单 / 撤单
    # ------------------------------------------------------------

    @staticmethod
    def _fill(el, text: str) -> None:
        """用户定版输入法：双击全选 → 一次 Backspace → 键入，零延迟"""
        el.click_input(double=True)
        el.type_keys("{BACKSPACE}", pause=0)
        if text:
            el.type_keys(text, pause=0)

    def _wait_autofill(self, el, timeout: float = 0.3) -> None:
        """等客户端对已接受代码自动填建议价落地（实测 <0.1s）再覆盖键入，
        防自动填晚到覆盖我们已键入的价格（正确性等待，非防御性延迟）"""
        if el is None:
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if (el.window_text() or "").strip():
                    return
            except Exception:
                return
            time.sleep(0.05)

    def place_order(self, code: str, status: str, price: str,
                    amount: str) -> dict:
        """双向委托页下单：填三框 → 唯一门控（可买/可卖≥0）→ 点提交

        无名称联动校验/弹窗扫描/模式切换（用户定版：最简路径）。
        委托后表单由客户端自动清空（3553 未勾选保障）。
        """
        window = self._ensure_home()
        form = self._FORMS[status]
        window.set_focus()
        code_el = self._cache["fields"].get(form["code"])
        if code_el is None or not self._alive(code_el):
            self._rebuild(self._window())
            code_el = self._cache["fields"].get(form["code"])
        self._fill(code_el, code)
        price_el = self._cache["fields"].get(form["price"])
        self._wait_autofill(price_el)  # 建议价落地后再覆盖（防晚到覆盖竞态）
        for key, value in (("price", price), ("amount", amount)):
            el = self._cache["fields"].get(form[key])
            if el is None or not self._alive(el):
                self._rebuild(self._window())
                el = self._cache["fields"].get(form[key])
            if el is None:
                raise ApiError(ErrorCode.CONTROL_NOT_FOUND,
                               f"双向委托页控件缺失 cid={form[key]} ({key})")
            self._fill(el, value)

        ready = self._cache["fields"].get(form["ready"])
        ready_text = ""
        if ready is not None and self._alive(ready):
            try:
                ready_text = (ready.window_text() or "").strip()
            except Exception:
                ready_text = ""
        if not self._READY_RE.match(ready_text):
            raise ApiError(
                ErrorCode.SERVER_UNAVAILABLE,
                f"「{'可买' if status == '1' else '可卖'}」数值未就绪"
                f"（读到 {ready_text or '空'}），已取消提交——极速模式唯一门控",
                suggestion="稍候重试；若持续为空请检查券商行情/资金通道或重新登录")
        self.logger.info(f"极速下单门控通过: "
                         f"{'可买' if status == '1' else '可卖'}={ready_text}")

        submit = self._cache["fields"].get(form["submit"])
        if submit is None or not self._alive(submit):
            raise ApiError(ErrorCode.CONTROL_NOT_FOUND,
                           f"提交按钮缺失 cid={form['submit']}")
        submit.click_input()
        self.logger.info(f"极速下单已提交: {code} {'买入' if status == '1' else '卖出'} "
                         f"{amount}@{price}（不等待弹窗，不回读确认）")
        return {"action": "买入" if status == "1" else "卖出", "mode": "限价",
                "code": code, "amount": amount, "price": price,
                "ready": ready_text, "submitted": True}

    def cancel(self, cancel_type: str = "A") -> dict:
        """同页撤单：全撤(30001)/撤买(30002)/撤卖(30003)，直点不等待

        先按 F8 对齐到委托标签（网格失焦时按键，无 F1/F2 红线）——
        全撤钮虽然各标签都在，但委托列表上下文缺届时可能空点（2026-10-12
        实测一笔「成功点击」未生效）。
        """
        window = self._ensure_home()
        cid = self._CANCEL[cancel_type]
        btn = self._cache["fields"].get(cid)
        if btn is None or not self._alive(btn):
            self._rebuild(self._window())
            btn = self._cache["fields"].get(cid)
        if btn is None:
            raise ApiError(ErrorCode.CONTROL_NOT_FOUND,
                           f"撤单按钮缺失 cid={cid}")
        window.set_focus()
        neutral = self._cache["fields"].get(1401)
        if neutral is not None and self._alive(neutral):
            try:
                neutral.click_input()  # 焦点挪出网格再发功能键
            except Exception:
                pass
        window.type_keys("{F8}", pause=0.05)
        time.sleep(0.3)
        btn = self._cache["fields"].get(cid)
        if btn is None or not self._alive(btn):
            self._rebuild(self._window())
            btn = self._cache["fields"].get(cid)
        btn.click_input()
        self.logger.info(f"极速撤单已点击: type={cancel_type}")
        return {"cancel_type": cancel_type, "clicked": True}

    # ------------------------------------------------------------
    # 页内查询（F6/F7/F8）与 F4 查询页
    # ------------------------------------------------------------

    # 各标签网格表头签名（防 F 键未生效时串台取错数据）
    _VIEW_SIGNATURES = {"positions": "股票余额", "trades": "成交时间",
                        "orders": "委托时间"}

    def query_tab(self, view: str) -> list:
        """F6 持仓 / F7 成交 / F8 委托 → 点网格正中 → Ctrl+C → 验证码 → TSV

        F 键必须在网格失焦时发送（网格持焦吞功能键，2026-10-12 实锤：
        上轮复制的中心点击留驻焦点，下轮 F8 被吞拿到持仓数据）——先点
        可买标签(1401 纯静态文本)把焦点挪出网格再发 F 键；表头签名断言
        防串台，失败自动重试一轮。
        """
        names = {"positions": "持仓", "trades": "成交", "orders": "委托(可撤)"}
        sig = self._VIEW_SIGNATURES[view]
        for attempt in (1, 2):
            window = self._ensure_home()
            window.set_focus()
            neutral = self._cache["fields"].get(1401)
            if neutral is not None and self._alive(neutral):
                try:
                    neutral.click_input()  # 焦点挪出网格（静态文本无副作用）
                except Exception:
                    pass
            window.type_keys(self._F_KEYS[view], pause=0.05)
            # 同标签二次查询数据滞后实锤（2026-10-12：下单后新活单不在列）
            # ——F5 强制拉新后再复制，0.4s 落定
            window.type_keys("{F5}", pause=0.05)
            time.sleep(0.4)
            with timed(f"极速页内查询[{names[view]}]", self.logger):
                header, rows = self._copy_grid(window)
            # 表头签名断言（空数据列表也有表头——可撤委托为 0 是合法状态）
            if any(sig in k for k in header):
                return rows
            self.logger.warning(f"页内查询[{names[view]}]第 {attempt} 次串台"
                                f"（表头无「{sig}」，rows={len(rows)}），重试")
        raise ApiError(ErrorCode.INTERNAL_ERROR,
                       f"页内查询[{names[view]}]连续串台（表头无「{sig}」）",
                       suggestion="重试一次；若持续串台请检查底部标签状态")

    def query_balance(self) -> dict:
        """资金余额：导航树点「资金股票」→ cid 批量读取（无验证码）→ 回主页"""
        self._ensure_home()
        self._click_nav_item("资金股票")
        time.sleep(0.25)
        window = self._window()
        want = set(BALANCE_FIELDS.values())
        found = {}
        for el in window.descendants():
            try:
                info = el.element_info
                if info.control_id in want and info.rectangle.width() > 0:
                    found[info.control_id] = el
            except Exception:
                continue
        result = {}
        for field_name, cid in BALANCE_FIELDS.items():
            el = found.get(cid)
            try:
                result[field_name] = (el.window_text() or "").strip() or None \
                    if el is not None else None
            except Exception:
                result[field_name] = None
        self._back_home()
        if all(v is None for v in result.values()):
            raise ApiError(ErrorCode.INTERNAL_ERROR,
                           "资金概览字段全部为空（资金股票页未就绪）")
        return result

    def query_full_orders(self) -> list:
        """F4 当日委托页全量委托（含已撤）——全量委托唯一查询口（用户定版）"""
        self._ensure_home()
        self._click_nav_item("当日委托")
        time.sleep(0.25)
        _, rows = self._copy_grid(self._window())
        self._back_home()
        return rows

    def _click_nav_item(self, name: str) -> None:
        """导航树点查询页（缓存 TreeItem，失效按名字重找）"""
        window = self._window()
        item = self._cache["nav_items"].get(name)
        if item is None or not self._alive(item):
            for el in window.descendants():
                try:
                    if el.element_info.name == name:
                        item = el
                        break
                except Exception:
                    continue
            if item is None:
                raise ApiError(ErrorCode.CONTROL_NOT_FOUND,
                               f"导航树未找到「{name}」")
            self._cache["nav_items"][name] = item
        window.set_focus()
        item.click_input()
        # 查询页与主页字段缓存互斥：离开主页即作废主页引用
        self._cache["fields"] = {}
        self._cache["grid"] = None

    def _back_home(self) -> None:
        nav = self._cache["nav"]
        if nav is not None and self._alive(nav):
            try:
                nav.click_input()
                time.sleep(0.3)
                self._rebuild(self._window())
                return
            except Exception as e:
                self.logger.warning(f"极速模式回主页失败（下次请求重导航）: {e}")
        self._cache["fields"] = {}

    # ------------------------------------------------------------
    # 网格复制（中心点击聚焦 + 双发 Ctrl+C + 验证码 + TSV）
    # ------------------------------------------------------------

    def _click_grid_center(self) -> None:
        """单击查询网格正中：交键盘焦点给网格（历史实锤：Ctrl+C 只对
        持焦控件生效）；同时在任何功能键前把焦点收进右侧，防全局跳页"""
        grid = self._cache["grid"]
        if grid is None or not self._alive(grid):
            self._rebuild(self._window())
            grid = self._cache["grid"]
        if grid is None:
            return
        r = grid.element_info.rectangle
        cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
        import win32api
        import win32con
        win32api.SetCursorPos((cx, cy))
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.05)

    def _copy_grid(self, window) -> tuple:
        """网格复制：返回 (表头列表, 数据行列表)——空列表也有表头（合法状态）"""
        grid = self._cache["grid"]
        if grid is None or not self._alive(grid):
            self._rebuild(window)
            grid = self._cache["grid"]
        if grid is None:
            raise ApiError(ErrorCode.CONTROL_NOT_FOUND, "未找到查询网格(1047)")

        self._click_grid_center()  # Ctrl+C 前置聚焦（用户定版规则）

        last_err = None
        for attempt in (1, 2):
            self.ws.clear_clipboard()
            self._ctrl_c_x2()
            try:
                self._solve_captcha_if_any(window)
            except ApiError as e:
                last_err = e
                continue
            data = self.ws.get_clipboard()
            if data and "\t" in data:
                return self._parse_tsv(data)
            last_err = ApiError(ErrorCode.OCR_FAILED,
                                f"第 {attempt} 次复制未获得表格数据"
                                "（验证码未过/焦点丢失）")
        raise last_err

    @staticmethod
    def _ctrl_c_x2() -> None:
        """keybd_event 双发 Ctrl+C（中文输入法吞首发，历史铁律）"""
        import win32api
        import win32con
        VK_CTRL, VK_C = win32con.VK_CONTROL, ord("C")
        for _ in range(2):
            win32api.keybd_event(VK_CTRL, 0, 0, 0)
            time.sleep(0.05)
            win32api.keybd_event(VK_C, 0, 0, 0)
            time.sleep(0.05)
            win32api.keybd_event(VK_C, 0, 2, 0)
            time.sleep(0.05)
            win32api.keybd_event(VK_CTRL, 0, 2, 0)
            time.sleep(0.1)

    def _find_captcha_dialog(self, main_hwnd: int):
        """同进程 #32770（主窗口子对话框优先，顶层兜底）→ 返回含 2405 的弹窗"""
        import win32gui
        import win32process

        _, pid = win32process.GetWindowThreadProcessId(main_hwnd)
        candidates = []

        def check(h, _extra):  # pywin32 枚举回调必须收 (hwnd, extra) 两参
            try:
                if not win32gui.IsWindowVisible(h):
                    return
                _, wpid = win32process.GetWindowThreadProcessId(h)
                if wpid != pid or win32gui.GetClassName(h) != "#32770":
                    return
                candidates.append(h)
            except Exception:
                pass

        try:
            win32gui.EnumChildWindows(main_hwnd, check, None)
        except Exception:
            pass
        try:
            win32gui.EnumWindows(check, None)
        except Exception:
            pass
        seen = set()
        for h in candidates:
            if h == main_hwnd or h in seen:
                continue
            seen.add(h)
            try:
                dlg = self._wrap_hwnd(h)
                for el in dlg.descendants():
                    try:
                        if el.element_info.control_id == self._CAPTCHA_IMAGE:
                            return dlg
                    except Exception:
                        continue
            except Exception:
                continue
        return None

    @staticmethod
    def _wrap_hwnd(hwnd):
        from pywinauto import Desktop
        return Desktop(backend="uia").window(handle=hwnd)

    def _solve_captcha_if_any(self, window) -> None:
        """验证码弹窗（~1.2s 预算内出现）：OCR → 点输入框 → 键入 → 确定"""
        deadline = time.time() + 1.2
        dlg = None
        while time.time() < deadline:
            dlg = self._find_captcha_dialog(window.handle)
            if dlg is not None:
                break
            time.sleep(0.05)
        if dlg is None:
            return  # 免验证码会话：剪贴板应已有数据

        img_el = edit_el = ok_el = cancel_el = None
        for el in dlg.descendants():
            try:
                cid = el.element_info.control_id
                if cid == self._CAPTCHA_IMAGE:
                    img_el = el
                elif cid == self._CAPTCHA_INPUT:
                    edit_el = el
                elif cid == self._CAPTCHA_OK:
                    ok_el = el
                elif cid == self._CAPTCHA_CANCEL:
                    cancel_el = el
            except Exception:
                continue
        if img_el is None or edit_el is None:
            self.logger.warning("弹窗缺验证码控件（2405/2404），按非验证码弹窗忽略")
            return

        cache_dir = self.config.get_logging_config().get("screenshot_dir",
                                                         "logs/screenshots")
        os.makedirs(cache_dir, exist_ok=True)
        image_path = os.path.join(cache_dir, "captcha.png")
        img_el.capture_as_image().save(image_path)
        digits = self._ocr.recognize(image_path) if self._ocr else None
        if not digits:
            self.logger.warning(f"极速模式 OCR 识别为空，点取消放弃本轮")
            if cancel_el is not None:
                cancel_el.click_input()
            raise ApiError(ErrorCode.OCR_FAILED, "验证码 OCR 识别为空")

        edit_el.click_input()  # 真实点击聚焦（弹窗输入框必需，历史定案）
        edit_el.type_keys(digits, pause=0)
        if ok_el is not None:
            ok_el.click_input()
        self.logger.info(f"极速模式验证码已提交: {digits}")
        # 等弹窗退场（验证通过即关；1.0s 轮询，clipboard 校验兜底）
        deadline = time.time() + 1.0
        while time.time() < deadline:
            try:
                if not self._alive(edit_el):
                    return
            except Exception:
                return
            time.sleep(0.1)

    @staticmethod
    def _parse_tsv(data: str) -> tuple:
        """TSV → (表头列表, 数据行列表)；只有表头 = 合法空列表"""
        lines = [ln for ln in data.replace("\r\n", "\n").split("\n") if ln.strip()]
        if not lines:
            return [], []
        header = lines[0].split("\t")
        rows = []
        for ln in lines[1:]:
            cells = ln.split("\t")
            rows.append({header[i]: cells[i] if i < len(cells) else ""
                         for i in range(len(header))})
        return header, rows
