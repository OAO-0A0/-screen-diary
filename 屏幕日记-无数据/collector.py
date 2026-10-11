"""
屏幕日记 —— 采集器

把「监测窗口」+「截图」+「写事件流」串起来，跑在后台线程里。

采集阶段**一次都不调用模型** —— 白天完全不占 GPU，这是这个项目的核心设计。
你按「我有空」的时候才批量分析。
"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime

from activity import ActivityWatcher, idle_seconds, window_signature
from config import (AWAY_IDLE_SECONDS, FOCUS_AUTO_OFF_SECONDS,
                    FOCUS_IDLE_SECONDS, load_config)
from screenshots import Shooter
from store import Store, today_str

# 多久写一条「键鼠有活动」的心跳。一天几百条，用来给统计提供活动证据
# （否则长时间不切窗口的那段时长会因为「没有证据」而被压短）。
HEARTBEAT_SECONDS = 120

class Collector:
    """
    后台采集。start() / stop()，status() 给界面查。
    """

    def __init__(self, logger=None):
        self.log_cb = logger or (lambda *a, **k: None)
        self._thread: threading.Thread | None = None
        self._stop_flag = False
        self._lock = threading.Lock()

        self.started_at: float = 0.0
        self.day: str = today_str()
        self.store = Store(self.day)
        self.shooter: Shooter = None

        self.events_written = 0
        self.shots_taken = 0
        self.last_proc = ""
        self.last_title = ""
        self.last_shot_at = 0.0
        self.last_event_at = 0.0
        self.current_idle = 0

        # 「离开电脑」的判定状态
        self._away_start: float | None = None      # 离开起点
        self._away_elapsed: int = 0                # 最近一次观测到的空闲时长
        self._just_returned: bool = False          # 下一个窗口是否算「回来后的第一个」
        # 上一张截图对应的「应用」（进程|窗口类）。同一个应用内不再重复截图，
        # 否则在资源管理器里翻目录会一路连拍（实测 15 个目录拍 14 张）。
        self._last_shot_app: str = ""
        # 已经滚到哪一天了（幂等保护，见 _rollover_if_needed）
        self._rolled_to: str = ""
        # 上一次写「键鼠有活动」心跳的时刻。
        #
        # 初始化成 0 或负数都不行：`now - 0` 和 `now - (-1)` 都远超阈值，
        # 于是采集一启动、第一次空闲采样就写一条心跳 —— 你什么都没干，
        # 却凭空多出一个当天的数据文件（这个坑我踩了两次，第一次改成 -1
        # 以为能挡住，其实一样会立刻触发）。
        #
        # 正确做法是**在 start() 里设成当前时刻**，让第一条心跳等满
        # HEARTBEAT_SECONDS 再写。这里先给 0，start() 会覆盖它。
        self._last_heartbeat: float = 0.0
        # 待定的窗口：先不落盘，等你醒着或换了窗口才写
        self._pending_proc: str = ""
        self._pending_title: str = ""
        self._pending_cls: str = ""
        self._pending_since: float = 0.0
        # 启动补记的空档是否已经写过离开事件（避免回来时写第二条重叠的）
        self._gap_already_written: bool = False
        # 专注模式：开着时把离开阈值拉长到 FOCUS_IDLE_SECONDS。
        # 界面线程会调 set_focus_mode()，采集线程会读，所以用锁保护。
        self._focus_mode: bool = False
        self._focus_lock = threading.Lock()
        # 到点自动关的时刻；0 表示没开
        self._focus_deadline: float = 0.0
        # 自动关闭时回调界面（让勾选框跟着取消）
        self.on_focus_change = None

    # ---------------- 专注模式 ----------------
    def away_idle_seconds(self) -> int:
        """当前生效的离开阈值。专注模式下是 2 小时。"""
        with self._focus_lock:
            focused = self._focus_mode
        return FOCUS_IDLE_SECONDS if focused else AWAY_IDLE_SECONDS

    def focus_mode(self) -> bool:
        with self._focus_lock:
            return self._focus_mode

    def focus_deadline(self) -> float:
        with self._focus_lock:
            return self._focus_deadline

    def focus_seconds_left(self) -> int:
        """专注模式还剩多少秒自动关闭；没开则 0。"""
        with self._focus_lock:
            if not self._focus_mode:
                return 0
            return max(0, int(self._focus_deadline - time.time()))

    def set_focus_mode(self, on: bool) -> bool:
        """
        开关专注模式。返回是否真的变了。

        打开时会设一个「自动关闭时刻」（FOCUS_AUTO_OFF_SECONDS 之后）。
        关掉时要顺手把「离开中」状态收尾：专注期间你可能已经空闲很久
        （但不算离开），一关掉阈值就回落到 10 分钟，那段空闲会立刻满足
        离开条件 —— 不收尾的话它的起点会被追溯到很久以前，把专注期间
        的时间整段吞掉。所以这里以「关掉的那一刻」为界收尾。
        """
        with self._focus_lock:
            if self._focus_mode == bool(on):
                return False
            self._focus_mode = bool(on)
            self._focus_deadline = (time.time() + FOCUS_AUTO_OFF_SECONDS) if on \
                else 0.0

        if not on:
            try:
                self._close_away_now()
            except Exception as e:
                self.log_cb(f"[专注] 收尾失败：{e}")
        return True

    def check_focus_expiry(self):
        """
        到点了就自动关掉专注模式。由 _on_idle 每次采样时调用。

        为什么用「采样时检查」而不是起一个定时器线程：多一个线程就多一处
        要同步的状态，而这里晚几十秒关掉没有任何影响。搭空闲采样的车最简单。
        """
        with self._focus_lock:
            if not self._focus_mode or not self._focus_deadline:
                return
            if time.time() < self._focus_deadline:
                return
        self.log_cb(f"[专注] 已满 "
                    f"{FOCUS_AUTO_OFF_SECONDS // 3600} 小时，自动关闭")
        self.set_focus_mode(False)
        cb = self.on_focus_change
        if cb:
            try:
                cb(False, "auto")
            except Exception:
                pass

    def _close_away_now(self):
        """以当前时刻为界，把「离开中」收尾并记录当前窗口。"""
        if self._away_start is None:
            return
        now = time.time()
        end = max(now, self._away_start + self._away_elapsed)
        if end > self._away_start and not self._gap_already_written:
            self.store.append_event(
                "idle", ts=self._away_start,
                extra={"idle": int(end - self._away_start),
                       "start": self._away_start, "end": end})
        self._gap_already_written = False
        self._away_start = None
        self._away_elapsed = 0
        self._just_returned = True
        self._last_shot_app = ""
        self._record_resume_window(now)
        self.log_cb("[专注] 已关闭，离开状态在此收尾")

    # ---------------- 生命周期 ----------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return False

        cfg = load_config()
        self.day = today_str()
        self.store = Store(self.day)
        self.shooter = Shooter(max_width=int(cfg.get("shot_max_width", 896)))
        self._stop_flag = False
        self.started_at = time.time()
        # 心跳基线设成「现在」，这样第一条心跳要等满 HEARTBEAT_SECONDS ——
        # 不这么做的话，启动后第一次空闲采样就会写一条（见 __init__ 的说明）。
        self._last_heartbeat = self.started_at

        # 补记上次关闭到本次启动之间的空档。
        #
        # 为什么必须补：程序关闭期间没有任何东西在记录「你不在」。重新打开后
        # 统计端只看到「上一条事件」和「新的心跳」，中间那片空白既不是活跃
        # 证据、也不是离开证据，就被**默认当成你在用**。
        # 实测：用 30 分钟 -> 关掉 -> 隔 2 小时 -> 再用 30 分钟，
        # 算出来是 3 小时 2 分（真实 1 小时），整整多出那 2 小时，而且静默无声。
        self._record_shutdown_gap()

        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="collector")
        self._thread.start()
        self.log_cb(f"[采集] 已开始，截图目录 data/shots/{self.day}")
        return True

    def _record_shutdown_gap(self):
        """
        把「上次采集结束」到「现在」这段写成一条离开事件。

        只写今天文件里的空档：跨天的空档由跨天分账负责，不该塞进今天的账上。
        （比如昨晚 23:50 关掉、今早 9:00 打开，这中间的夜不属于今天。）
        """
        try:
            events = self.store.read_events()
        except Exception:
            return
        if not events:
            return

        # 今天已有事件的最后时刻。用 max 而不是取最后一行 ——
        # 文件里的事件不保证严格按时间排列（心跳和窗口事件是两条写入路径）。
        last_ts = 0.0
        for e in events:
            ts = e.get("ts")
            if ts:
                last_ts = max(last_ts, float(ts))
        if not last_ts:
            return

        # 只处理「今天」的空档：最后一条事件的日期必须就是今天
        try:
            if datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d") != self.day:
                return
        except Exception:
            return

        gap = self.started_at - last_ts
        if gap < AWAY_IDLE_SECONDS:
            return          # 空档太短，正常运行中的空闲检测会管它

        self.store.append_event(
            "idle", ts=last_ts,
            extra={"idle": int(gap), "start": last_ts,
                   "end": self.started_at, "shutdown_gap": True})
        # 同时把采集器置成「离开中」。
        #
        # 为什么不能只写事件就走：回来时 `_on_idle` 靠 `_away_start` 是否
        # 为 None 来判断「这是回来」，然后才会补记当前窗口。如果这里不设，
        # 那条路径不会触发，回来的那段时间照样丢（实测丢过 30 分钟）。
        self._away_start = last_ts
        self._away_elapsed = int(gap)
        # 标记「离开区间已经写过了」：回来时不再重复写一条重叠区间
        self._gap_already_written = True
        self.log_cb(f"[采集] 上次关闭到现在的空档 {int(gap // 60)} 分钟，"
                    f"已记为离开")

    def stop(self):
        self._stop_flag = True
        if self._thread:
            self._thread.join(timeout=8)
        # 收尾：把待定的窗口落盘。
        # 不做的话会丢两段时长 —— 最后那个窗口（从切过去到关程序）
        # 整段都不会进日志。你「开程序时就在看文献、一直没切」那种情况
        # 全靠这一下才能记下来（实测踩过，丢过 3 小时）。
        try:
            self._flush_pending_window()
        except Exception as e:
            self.log_cb(f"[采集] 收尾时出错：{e}")
        self.log_cb("[采集] 已停止")
        return True

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive()
                    and not self._stop_flag)

    def status(self) -> dict:
        up = time.time() - self.started_at if self.started_at and self.running() else 0
        return {
            "running": self.running(),
            "seconds": int(up),
            "events": self.events_written,
            "shots": self.shots_taken,
            "proc": self.last_proc,
            "title": self.last_title,
            "idle": self.current_idle,
        }

    # ---------------- 采集线程 ----------------
    def _run(self):
        cfg = load_config()
        watcher = ActivityWatcher(
            on_change=self._on_window_change,
            poll_seconds=float(cfg.get("poll_seconds", 3)),
            settle_seconds=float(cfg.get("settle_seconds", 5)),
            logger=self.log_cb,
            on_idle=self._on_idle,
            away_idle_seconds=AWAY_IDLE_SECONDS,
            focus_idle_seconds=FOCUS_IDLE_SECONDS,
        )
        # 让监测器能实时问到专注模式是否开着（阈值随它变）
        watcher.is_focus = self.focus_mode
        watcher.prime()
        watcher.run()

    def _is_away(self) -> bool:
        """
        你是否已经不在电脑前。

        用两个条件判断，取更早生效的那个：
          * 已经确认进入离开状态（空闲跨过阈值）
          * 当前空闲已达「离开阈值的一半」且还在涨 —— 提前闭嘴，
            免得你走时随手 Alt+Tab 那几下被记成一段活动

        阈值随专注模式变化：专注时拉长到 2 小时，「看网课 40 分钟没动」
        不再被判成离开（否则实测那 40 分钟会被记成 1 分钟）。
        """
        if self._away_start is not None:
            return True
        return self.current_idle >= self.away_idle_seconds() // 2

    def _on_window_change(self, proc: str, title: str, cls: str = ""):
        """窗口稳定切换了：记一条事件，并拍一张图。"""
        if self._is_away():
            # 你已经不在电脑前，这期间的窗口变化不算你的活动
            return
        self._flush_pending_window()
        self._pending_proc = proc
        self._pending_title = title
        self._pending_cls = cls
        self._pending_since = time.time()

    def _flush_pending_window(self):
        """
        把待定的窗口写成一条事件。

        为什么要「待定」而不是立刻写：你离开电脑时，最后一个窗口会被无限累加
        （日记软件停在桌面上，它就把「你盯着它看」算成使用时间）。
        所以窗口先存着不写 —— 只有确认你还醒着，或者你又切了窗口，才落盘。
        真走了的话，这条会变成「离开」的起点，被 _close_away_at 截断。
        """
        if not self._pending_proc:
            return

        # 先把属于「旧的一天」的东西落盘，再检查是否跨天。
        # 顺序不能反：这条窗口事件的时间戳是过去那天的，
        # 如果先切换 store，它就会被写进新一天的文件里。
        proc = self._pending_proc
        title = self._pending_title
        cls = self._pending_cls
        start_ts = self._pending_since
        # 专注模式标记：统计端靠它知道「这段一律算活跃，不要判离开」。
        # 取落盘这一刻的状态（窗口存进来时可能还没开专注，差别只在几秒内）。
        focus = self.focus_mode()
        self._pending_proc = ""
        self._pending_title = ""
        self._pending_cls = ""

        now = time.time()
        shot_name = ""

        # ---- 截图去重：只有「换了个应用」才拍 ----
        #
        # 为什么不能只看标题变没变：文件资源管理器里翻目录，
        # 进程和窗口类完全没变，只是标题里的路径在变。
        # 实测每 6 秒切一个目录，翻 15 个目录会拍 14 张 ——
        # 而这 14 张对「你在干什么」几乎没有新增信息。
        #
        # 所以按「进程 + 窗口类」当应用标识：同一个应用内换标签页、
        # 换目录都不再重复拍。窗口事件照旧每条都记（标题里有信息）。
        #
        # 从「离开」回来时 _last_shot_app 已被清空，所以回来第一张必拍。
        app_key = f"{(proc or '').lower()}|{(cls or '').lower()}"
        should_shoot = (app_key != self._last_shot_app)

        # shooter 可能还没建好（比如采集刚启动），别让截图失败拖累记录
        if should_shoot and not self._is_away() and self.shooter is not None:
            shot, info = self.shooter.grab(self.store.shot_dir())
            if shot:
                shot_name = shot.name
                self.shots_taken += 1
                self.last_shot_at = now
                self._last_shot_app = app_key
                self.log_cb(f"[截图] {shot.name}  {info.get('kb',0)}KB "
                            f"{info.get('ms',0)}ms  ({proc})")

        # 从「离开」回来后的第一个窗口打标记：统计时用它当离开区间的终点
        extra = {"awake": True} if self._just_returned else None
        if cls:
            extra = extra or {}
            extra["cls"] = cls          # 记下窗口类，方便以后排查识别问题

        if focus:
            extra = extra or {}
            extra["focus"] = True

        self.store.append_event("window", proc=proc, title=title,
                                shot=shot_name, ts=start_ts, extra=extra)
        self.events_written += 1
        self.last_event_at = now
        self.last_proc = proc
        self.last_title = title
        self._just_returned = False

        # 落盘完成后再处理跨天。放在这里是因为上面的截图要进旧那天的目录。
        self._rollover_if_needed()

    # ---------------- 跨天 ----------------
    def _rollover_if_needed(self) -> bool:
        """
        日期变了就切换 store / 截图目录。

        为什么需要这个：采集循环是常驻的，可能一连跑好几天。
        如果日期只在启动时取一次，跨过午夜之后的活动会一直写进
        前一天的账上（实测确认过），第二天分析出来的两天数据是混的。
        """
        today = today_str()
        if today == self.day:
            return False
        # 幂等保护：一次跨天只切一次。
        # 不加这个的话，窗口事件和空闲采样都会各自检测一次，
        # 于是同一天里连写好几条 rollover 标记（实测写了 4 条）。
        if getattr(self, "_rolled_to", "") == today:
            return False
        self._rolled_to = today

        old = self.day
        self._flush_pending_window_before_rollover()
        self.day = today
        self.store = Store(today)

        # 在新一天记一条标记，说明这是采集在跨天处接续的。
        # 否则新一天的第一个事件会被当成「你从那一刻开始用电脑」，
        # 中间从午夜到那一刻的空档算不出归属。
        self.store.append_event("rollover", extra={"from": old})
        self.last_shot_app = ""          # 新一天第一张必拍
        self.log_cb(f"[跨天] 已切换到 {today}（上一天 {old} 的记录已封存）")
        return True

    def _flush_pending_window_before_rollover(self):
        """
        跨天前把待定窗口落盘。

        单独写一个是因为 _flush_pending_window 末尾会调 _rollover_if_needed，
        而 _rollover_if_needed 又要调它 —— 不分开就是无限递归。
        """
        if not self._pending_proc:
            return
        proc = self._pending_proc
        title = self._pending_title
        cls = self._pending_cls
        start_ts = self._pending_since
        self._pending_proc = ""
        self._pending_title = ""
        self._pending_cls = ""

        extra = {"cls": cls} if cls else None
        if self.focus_mode():
            extra = extra or {}
            extra["focus"] = True
        self.store.append_event("window", proc=proc, title=title,
                                ts=start_ts, extra=extra)
        self.events_written += 1

    def _record_resume_window(self, now: float):
        """
        从「离开」状态回来时，把当前前台窗口记成一条新事件。

        它带 awake 标记，统计端用它当离开区间的**终点**，也是这段重新
        开始活动的**起点**。不写的话，离开区间会一直延伸到采集结束，
        而回来的那段时间整段丢失（签名没变，tick() 不会产生事件）。
        """
        try:
            from activity import foreground_window, should_ignore
            title, proc, pid, cls = foreground_window()
            if should_ignore(proc, title, cls):
                return
            extra = {"awake": True}
            if cls:
                extra["cls"] = cls
            if self.focus_mode():
                extra["focus"] = True
            self.store.append_event("window", proc=proc, title=title,
                                    ts=now, extra=extra)
            self.events_written += 1
            self.last_event_at = now
            self.last_proc = proc
            self.last_title = title
            self.log_cb(f"[回来] 已记录当前窗口：{proc}")
        except Exception as e:
            self.log_cb(f"[回来] 记录当前窗口失败：{e}")

    def _on_idle(self, idle: int):
        # 空闲采样每分钟都会来一次，是跨天检测的可靠时机
        # （窗口事件只在切换时才有，你整晚不动它就永远等不到）。
        self._rollover_if_needed()
        # 专注模式到点了就自动关（搭这次采样的车，不另起线程）
        self.check_focus_expiry()
        self.current_idle = idle
        now = time.time()

        if idle >= self.away_idle_seconds():
            if self._away_start is None:
                # 刚进入「离开」状态。离开起点取两个估计里**更晚**的那个：
                #   * 现在 - 已空闲时长（你最后一次动键鼠的时刻）
                #   * 最后那个窗口的起点
                #
                # 为什么取更晚：空闲回推点理论上不可能早于你最后一次切换窗口
                # 的时间。取更早（min）会写出「离开起点早于最后一次操作」的
                # 区间，这个区间随后会把真实在用的时间整段吞成离开
                # —— 实测踩过，直接吞掉了一段 30 分钟的阅读。
                by_idle = now - idle
                by_window = self._pending_since or self._last_event_at or by_idle
                self._away_start = max(by_idle, by_window)
                self._away_elapsed = int(now - self._away_start)
                self._just_returned = False
                # 把待定的窗口落盘 —— 它成了「离开」的起点，回来时会被截断。
                # 注意：这里**不写**离开区间，因为还不知道你什么时候回来。
                # 早写会导致区间被拆成好几段（实测踩过）。
                self._flush_pending_window()
                self.log_cb(f"[离开] 检测到你离开了（已空闲 {idle // 60} 分钟）")
            else:
                self._away_elapsed = int(now - self._away_start)   # 还在离开中
            return

        # ---- 空闲恢复正常：说明你回来了 ----
        if self._away_start is not None:
            # 取「你回来那一刻」和「最后一次观测到的空闲终点」里更晚的
            end_by_elapsed = self._away_start + self._away_elapsed
            end = max(now, end_by_elapsed)
            # 启动时补记过的那段空档已经写过一条离开事件了。
            # 这里再写一条会得到两条起点相同、终点不同的重叠区间
            # （实测 T+1800~T+9000 和 T+1800~T+9060 同时存在），
            # 统计端会把同一段时间扣两次。所以那种情况只更新终点。
            if self._gap_already_written:
                self._gap_already_written = False
                self.log_cb("[回来] 离开时段结束（启动时已补记过起点）")
            elif end > self._away_start:
                # 记下写这条时的阈值：专注模式下阈值是 2 小时，
                # 一条 30 分钟的「离开」在那时不成立，统计端要能识别出来
                # 并忽略它（否则专注期间的小停顿会被扣成离开）。
                self.store.append_event(
                    "idle", ts=self._away_start,
                    extra={"idle": int(end - self._away_start),
                           "start": self._away_start,
                           "end": end,
                           "threshold": self.away_idle_seconds(),
                           "focus": self.focus_mode()})
                self.log_cb(f"[回来] 离开时段已记录，约 "
                            f"{int((end - self._away_start) // 60)} 分钟")
            self._away_start = None
            self._away_elapsed = 0
            self._just_returned = True
            # 回来后第一张必拍：你离开前后的屏幕内容通常完全不同
            self._last_shot_app = ""
            # 把「你回来时正在用的那个窗口」记一条。
            #
            # 为什么必须写：窗口事件只在**签名变化**时产生。你离开期间用的
            # 还是同一个窗口（比如一直读同一篇 PDF），回来时签名没变，
            # tick() 不会产生任何事件 —— 于是这段回来的时间既没有活跃证据，
            # 也没有东西来收口上面的离开区间，整段凭空消失。
            # 实测：用 30 分钟 -> 关程序 2 小时 -> 再打开用 30 分钟，
            # 第二段 30 分钟完全没算进去（活跃只有 30 分，实际 1 小时）。
            self._record_resume_window(now)
            return

        # ---- 你还醒着：定期写一条心跳 ----
        #
        # 为什么要写：窗口事件只在「切换窗口」时产生。如果你开着同一个窗口
        # 连续用很久（比如看 3 小时文献、一直没切），那就没有任何事件能
        # 证明你还在用电脑 —— 统计时最后一段会因为没有活动证据而被压成
        # 60 秒，几个小时的时长凭空蒸发（实测踩过）。
        #
        # 心跳就是「这个时刻键鼠有活动」。一天几百条，几 KB，可以忽略。
        #
        # 只在 idle == 0 时写：这才真正代表「你刚动过键鼠」。
        # 早期版本不看 idle，结果程序一启动就写一条心跳（当下空闲本来就是 0，
        # 而且 `now - 0` 一定超过阈值），于是每个开过程序的日子都会多出一个
        # 只含心跳的数据文件，看着像「那天有活动」其实你什么都没干。
        # 改成 idle == 0 之后，「启动即写」这条路也自然被堵住了 ——
        # 真正空闲时 idle 会是几十秒，不满足条件。
        if idle == 0 and now - self._last_heartbeat >= HEARTBEAT_SECONDS:
            self._last_heartbeat = now
            hb_extra = {"idle": 0, "heartbeat": True}
            # 专注期间的心跳也带标记：统计端知道这段一律算活跃
            if self.focus_mode():
                hb_extra["focus"] = True
            self.store.append_event("idle", extra=hb_extra)
