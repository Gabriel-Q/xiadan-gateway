"""xiadan-gateway 启动入口

启动顺序:
1. 单实例互斥锁检查
2. 加载配置
3. 初始化 Logger
4. 预加载 ddddocr
5. 启动窗口监控
6. 启动 waitress 生产服务器（主线程）
7. 优雅关闭（Ctrl+C 时停止监控、等待队列排空）
"""
import signal
import sys
import threading
import time

import win32api
import win32event
import winerror

from src.api.routes import create_app
from src.core.ocr import OcrService
from src.models.config import AppConfig
from src.services.window_monitor import WindowMonitor
from src.utils.logger import Logger


# 单实例互斥锁名称
MUTEX_NAME = "Global\\xiadan-gateway_Single_Instance_Mutex"


def setup_console_utf8():
    """Windows 控制台 UTF-8 支持：中文字符正常显示

    乱码根源: Windows 控制台默认代码页 936(GBK)，且重定向时
    stdout 用 ANSI 编码写入。日志文件本身是 UTF-8（无问题），
    乱码只发生在控制台/重定向输出。

    修复:
    1. SetConsoleOutputCP(65001): 前台运行时控制台按 UTF-8 解码
    2. sys.stdout/stderr reconfigure: 后台运行/重定向时输出 UTF-8 字节
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def check_single_instance():
    """检查是否已有实例在运行"""
    mutex = win32event.CreateMutex(None, False, MUTEX_NAME)
    last_error = win32api.GetLastError()
    if last_error == winerror.ERROR_ALREADY_EXISTS:
        print("程序已在运行，请勿重复启动", file=sys.stderr)
        sys.exit(0)
    return mutex


def main():
    # 0. 控制台 UTF-8（中文日志正常显示）
    setup_console_utf8()

    # 1. 单实例检查
    mutex = check_single_instance()

    # 优雅关闭事件
    shutdown_event = threading.Event()
    monitor = None

    try:
        # 2. 加载配置
        config = AppConfig()
        logger = Logger.get_instance()
        # 应用日志级别（默认 INFO，debug 日志仅在排查时开启）
        logger.set_level(config.get_logging_config().get("level", "INFO"))

        # 2.5 配置校验：非法配置直接中止，避免启动后运行期爆炸
        config_errors = config.validate()
        if config_errors:
            logger.error("配置校验失败，启动中止。请修复 config/app_config.json：")
            for err in config_errors:
                logger.error(f"  - {err}")
            logger.error("修复后重新启动")
            sys.exit(1)

        logger.info("=" * 60)
        logger.info("xiadan-gateway 启动中...")
        logger.info("=" * 60)

        # 3. 检查 xiadan.exe 路径
        trading_paths = config.get_trading_app_paths()
        if not trading_paths:
            logger.warning(
                "未配置 xiadan.exe 路径！请在 config/app_config.json 中设置 trading_app_paths"
            )
        else:
            logger.info(f"交易程序路径: {trading_paths}")

        # 4. 预热 OCR 引擎（轻量模板匹配；ddddocr 可选）
        ocr_config = config.get_ocr_config()
        if ocr_config.get("warmup_on_start", True):
            ddddocr_enabled = ocr_config.get("ddddocr_enabled", False)
            logger.info("预热 OCR 引擎...")
            ocr = OcrService.get_instance()
            ocr.configure(ddddocr_enabled=ddddocr_enabled)
            if not ocr.warmup():
                logger.warning(
                    "OCR 引擎预热失败，查询持仓/成交时若遇到验证码将无法处理"
                )
            else:
                coverage = len(ocr.lightweight_coverage)
                logger.info(f"OCR 引擎就绪，覆盖 {coverage}/10 个数字")

        # 5. 启动窗口监控
        monitor_config = config.get_window_monitor_config()
        if monitor_config.get("enabled", True) and trading_paths:
            monitor = WindowMonitor(check_interval=monitor_config.get("check_interval", 2))
            monitor.start(trading_paths)
        else:
            logger.info("窗口监控已禁用")

        # 5.5 清理过期截图
        from src.utils.screenshot import ScreenshotUtil
        logging_cfg = config.get_logging_config()
        screenshot_util = ScreenshotUtil(
            logging_cfg.get("screenshot_dir", "logs/screenshots")
        )
        screenshot_util.cleanup_old_screenshots()

        # 6. 创建 Flask 应用
        app = create_app()

        # 获取服务监听配置
        host = config.get_host()
        port = config.get_port()

        # 获取任务队列配置
        queue_config = config.get_task_queue_config()
        watchdog_timeout = queue_config.get("watchdog_timeout_seconds", 30)

        # 认证状态提示
        auth_config = config.get_auth_config()
        auth_enabled = auth_config.get("enabled", False) and bool(auth_config.get("token"))

        # HTTP 服务并发配置（waitress 线程池 / 业务闸门，日志与 serve 共用）
        api_config = config.get_api_config()

        logger.info("-" * 60)
        logger.info("xiadan-gateway 启动完成")
        logger.info(f"HTTP 服务: http://{host}:{port}")
        logger.info(f"健康检查: GET  http://{host}:{port}/health")
        logger.info(f"认证已启用: {auth_enabled}")
        logger.info(f"下单接口: POST http://{host}:{port}/orders")
        logger.info(f"持仓查询: GET  http://{host}:{port}/positions")
        logger.info(f"资金查询: GET  http://{host}:{port}/account/balance")
        logger.info(f"撤单接口: POST http://{host}:{port}/orders/cancel-all")
        logger.info(f"队列状态: GET  http://{host}:{port}/queue/status")
        logger.info(f"看门狗超时: {watchdog_timeout}s, 推荐客户端 timeout: {watchdog_timeout + 10}s")
        logger.info(
            f"服务器: waitress (线程={api_config.get('server_threads', 16)}, "
            f"业务并发上限={api_config.get('business_max_concurrent', 12)}, "
            f"差额预留管理端点)")
        logger.info("-" * 60)

        # 6.5 极速模式启动引导：默认跳转双向委托页 + 三勾选框确保未勾选。
        # 后台线程延迟 3s（让开客户端登录绘制期），经 TaskQueue 串行执行；
        # 失败不阻塞服务——每个 /fast 请求入口都有 ensure_home 懒重试。
        def _fast_bootstrap():
            try:
                # 严格隔离：order.fast_mode=false 时引导完全不执行，
                # 不触碰客户端（传统流程保持默认 F1 起始态）
                if not AppConfig().get_order_config().get("fast_mode"):
                    logger.info("极速模式未启用（order.fast_mode=false），跳过启动引导")
                    return
                from src.api.task_queue import TaskQueue
                from src.services.fast_trader import FastTrader
                from src.services.window_service import WindowService
                ws = WindowService()
                # 等待券商登录：轮询交易窗口出现（5s 间隔，最长 30 分钟）——
                # 先启动网关后登录客户端的场景由本循环覆盖；登录检测后引导
                # 仍失败（或超时放弃）则由首个极速任务懒引导兜底
                logger.info("极速模式启动引导：等待券商登录（每 5s 检测，最长 30 分钟）")
                deadline = time.time() + 1800
                detected = False
                while time.time() < deadline:
                    try:
                        if ws.get_trading_window() is not None:
                            detected = True
                            break
                    except Exception:
                        pass
                    time.sleep(5)
                if not detected:
                    logger.warning("极速模式启动引导放弃：30 分钟内未检测到登录窗口"
                                   "（首个极速任务会懒引导兜底）")
                    return
                time.sleep(3)  # 检测到登录后延时 3s，等界面元素绘制稳定
                logger.info("检测到券商登录，执行极速模式启动引导")
                TaskQueue.get_instance().submit(
                    func=lambda: FastTrader().bootstrap(),
                    task_name="fast_bootstrap", params={}, timeout=30)
            except Exception as e:
                logger.warning(f"极速模式启动引导失败（首个极速任务会懒引导兜底）: {e}")

        threading.Thread(target=_fast_bootstrap, daemon=True,
                         name="fast-bootstrap").start()
        logger.info("极速模式: /fast/* 已就绪（启动引导已排队）")

        # 7. 注册优雅关闭信号处理
        def _graceful_shutdown(signum, frame):
            logger.info("\n收到关闭信号，正在优雅停机...")
            shutdown_event.set()
            if monitor:
                monitor.stop()
            logger.info("窗口监控已停止，服务已关闭")
            sys.exit(0)

        signal.signal(signal.SIGINT, _graceful_shutdown)
        signal.signal(signal.SIGTERM, _graceful_shutdown)

        # 8. 启动 waitress 生产服务器
        # server_threads 必须大于业务并发上限（差额留给管理端点，见
        # api.admission_gate）；配合业务闸门防任务风暴饿死管理面
        from waitress import serve
        serve(app, host=host, port=port,
              threads=int(api_config.get("server_threads", 16)),
              channel_timeout=watchdog_timeout + 15)

    finally:
        if monitor:
            monitor.stop()
        if mutex:
            win32api.CloseHandle(mutex)


def dev():
    """开发模式（热加载）"""
    import hupper
    print("开发模式：热加载已启用")
    reloader = hupper.start_reloader("main.main")
    reloader.watch_files("**/*.py")


if __name__ == "__main__":
    if "--dev" in sys.argv:
        dev()
    else:
        main()
