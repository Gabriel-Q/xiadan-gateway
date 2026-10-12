"""业务并发准入闸门——管理端点轻量线程池的容量预留

背景（2026-10-12 过载压测实锤）：waitress 线程池大小固定（main.py 经
api.server_threads 配置），任务风暴下全部工作线程停泊在 TaskQueue.submit
的同步 event.wait（最长 timeout+10s），管理端点（/health、/queue/status）
抢不到线程，实测 15s+ 无响应——恰在最需要观测队列状态的时刻失去可观测性。

本闸门把物理线程池一分为二：业务请求并发数封顶（业务轻量池），剩余
线程容量永久保留给管理端点（管理轻量池）；超限业务请求立即以 QUEUE_FULL
语义拒绝（毫秒级放线程，不排队不停泊），客户端沿用既有 QUEUE_FULL 退避
重试策略即可，API 契约零变化。管理端点在 before_request 直接放行，永不
消耗业务并发额度。

与 TaskQueue(max_size) 两道闸的关系：本闸门在外层（HTTP 并发），队列
容量在内层（任务积压）。常规配置下本闸门先触发（business_max_concurrent
< max_size），拒绝语义与队列满一致，对客户端透明。
"""
import threading


class BusinessGate:
    """业务请求并发闸门：线程安全的名额计数器

    acquire/release 必须严格配对（路由层经 flask.g 在 teardown 中释放，
    异常路径也保证释放）；release 对未 acquire 的调用是安全空操作。
    """

    def __init__(self, max_concurrent: int):
        if not isinstance(max_concurrent, int) or isinstance(max_concurrent, bool) \
                or max_concurrent < 1:
            raise ValueError(f"max_concurrent 必须是 ≥1 的整数: {max_concurrent!r}")
        self._max = max_concurrent
        self._lock = threading.Lock()
        self._active = 0

    @property
    def max_concurrent(self) -> int:
        return self._max

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def try_acquire(self) -> bool:
        """占一个业务并发名额；满员立即 False（调用方毫秒级快速拒绝）"""
        with self._lock:
            if self._active >= self._max:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._active > 0:
                self._active -= 1
