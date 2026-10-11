"""
屏幕日记 —— 前台窗口与进程监测

三个 Win32 调用就够：GetForegroundWindow + GetWindowTextW +
QueryFullProcessImageNameW（另外加一个 GetClassNameW，因为系统自带应用的
进程名不足以区分它们）。

开销实测：每秒调一次，常驻内存约 28 MB、CPU 基本为 0，做日志统计完全够用。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import time

# 每隔这么久记一条键鼠空闲状态（用来判断是不是离开电脑了）
IDLE_SAMPLE_SECONDS = 60

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# ---------------------------------------------------------------
#  窗口类名：判断「这是不是一个真正的应用窗口」的关键依据
#
#  为什么需要它 —— 光看进程名会错得很离谱：
#    * explorer.exe 同时是桌面外壳和文件资源管理器窗口
#    * 所有 UWP 应用（设置、计算器、照片、应用商店…）的前台进程
#      都叫 applicationframehost.exe，进程名区分不出是哪个
#  只能靠窗口类 + 标题来分辨。
# ---------------------------------------------------------------

# 桌面外壳：这些不是「你在用某个软件」，不记
#
# 注意：'windows.ui.core.corewindow' 故意不在这里。
# 这个类同时用于系统提示和应用窗口（新式「设置」就是它），
# 放进这份名单会导致「设置」永远识别不出来（实测踩过）。
# 它在 should_ignore() 里单独判断。
SHELL_CLASSES = {
    "progman",                      # 桌面
    "workerw",                      # 桌面壁纸层
    "shell_traywnd",                # 任务栏
    "shell_secondarytraywnd",       # 副屏任务栏
    "foregroundstaging",            # 前台切换用的隐藏窗口
    "multitaskingviewframe",        # 任务视图
    "tasklistthumbnailwnd",         # 任务栏缩略图
}

# 文件资源管理器窗口的类（这类要记，因为它就是你打开的文件夹）
EXPLORER_CLASSES = {"cabinetwclass", "explorewclass"}

# UWP 应用外框：标题就是应用名
UWP_FRAME_CLASSES = {"applicationframewindow"}


def foreground_window() -> tuple[str, str, int, str]:
    """
    返回 (窗口标题, 进程名小写, 进程号, 窗口类名)。
    任何异常都返回空值，绝不抛出去 —— 监测线程不能因为一次 API 失败就死。
    """
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "", "", 0, ""

        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value or ""

        cls_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls_buf, 256)
        cls = (cls_buf.value or "").lower()

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return title, "", 0, cls

        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return title, "", pid.value, cls
        try:
            size = wintypes.DWORD(1024)
            name_buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(
                    handle, 0, name_buf, ctypes.byref(size)):
                return (title, os.path.basename(name_buf.value).lower(),
                        pid.value, cls)
            return title, "", pid.value, cls
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return "", "", 0, ""


def is_idle(seconds: int = 120) -> bool:
    """系统是否已经闲置（键鼠无操作）—— 用来判断你是不是离开电脑了。"""
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return False
        millis = kernel32.GetTickCount() - info.dwTime
        return millis > seconds * 1000
    except Exception:
        return False


# 启动时如果键鼠已经空闲超过这么久，就不把「当前窗口」算成你正在用。
#
# 比「离开」阈值（config.AWAY_IDLE_SECONDS = 600 秒）严格得多，因为这里
# 判断的是启动那一瞬间你人在不在。取宽了会把「开了程序就去泡茶」记成活动。
PRIME_IDLE_LIMIT = 120


def idle_seconds() -> int:
    """键鼠多久没动了，单位秒。"""
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return 0
        return max(0, int((kernel32.GetTickCount() - info.dwTime) / 1000))
    except Exception:
        return 0


def should_ignore(proc: str, title: str = "", cls: str = "") -> bool:
    """
    这个前台窗口该不该忽略？

    判定顺序（顺序很重要，别调换）：
      1. 桌面外壳 / 系统级窗口 -> 忽略
         （否则「你回到桌面」会被当成一种活动）
      2. 没有标题 -> 忽略
         （隐藏窗口、壳窗口。实测 explorer.exe 会带一个空标题的
           ApplicationFrameWindow，就是这个）
      3. 明确的系统噪音进程 -> 忽略
      4. 其余一律记录
         —— 包括 explorer.exe 的文件管理器窗口、以及
            applicationframehost.exe 承载的 UWP 应用（设置、计算器…）

    关键变化：以前是「按进程名排除」，所以 explorer.exe / systemsettings.exe /
    applicationframehost.exe 全被一刀砍掉，导致文件管理器和设置都识别不出来。
    现在改成「按窗口类 + 有没有标题」判断，这三类都能正常记录。
    """
    p = (proc or "").lower()
    t = (title or "").strip()
    c = (cls or "").lower()

    # 1) 桌面外壳：桌面、任务栏、任务视图
    if c in SHELL_CLASSES or c.startswith("xaml_explorer_"):
        return True

    # 2) Windows.UI.Core.CoreWindow 要单独判断。
    #    这个类既用于系统提示（音量、输入法），也用于「设置」这类
    #    新式应用窗口。区别在于：系统提示是壳，应用是我们要记的。
    #    实测「设置」窗口没有标题，所以不能靠标题分辨，只能按进程名。
    if c == "windows.ui.core.corewindow":
        return p not in UW_APP_PROCESSES

    # 3) 没有标题的窗口不是「你能看见的应用」
    if not t:
        return True

    # 4) 系统噪音进程
    if p in NOISE_PROCESSES:
        return True

    # 5) 其余都记
    return False


# Windows.UI.Core.CoreWindow 这个类下面，哪些进程算真应用窗口
UW_APP_PROCESSES = {
    "systemsettings.exe",       # 设置（实测没有标题）
    "calculator.exe",           # 计算器
    "applicationframehost.exe", # UWP 外框
    "windowscamera.exe", "photos.exe", "mspaint.exe", "snippingtool.exe",
    "microsoftstore.exe", "winstore.app.exe",
    "windows.immersivecontrolpanel.exe",
}

# 明确的系统噪音：这些进程不会有你想记录的应用窗口
NOISE_PROCESSES = {
    "lockapp.exe", "logonui.exe", "searchhost.exe", "shellexperiencehost.exe",
    "startmenuexperiencehost.exe", "textinputhost.exe",
    "sihost.exe", "usoclient.exe", "backgroundtaskhost.exe",
    "runtimebroker.exe", "dwm.exe", "csrss.exe", "winlogon.exe",
    # 程序自己 —— 你关掉别的窗口回到桌面时前台就是它，
    # 不排除掉的话它会把「你盯着日记软件发呆」记成你在用 Python
    "python.exe", "pythonw.exe",
}


def window_signature(proc: str, title: str, cls: str = "") -> str:
    """用于判断「算不算换了个窗口」。窗口类也要算进去。"""
    return (f"{(proc or '').lower()}|{(title or '').strip()}|"
            f"{(cls or '').lower()}")


class ActivityWatcher:
    """
    盯着前台窗口，窗口一变（并且稳定下来）就回调。

    稳定判定：切换后连续 settle_seconds 秒都是同一个窗口，才算真的换了。
    这样 Alt+Tab 飞快划一圈不会触发一堆事件。

    另外每隔 IDLE_SAMPLE_SECONDS 回调一次 on_idle(空闲秒数)，
    用来判断你是不是离开电脑了 —— 这是唯一可靠的依据。
    """

    def __init__(self, on_change, poll_seconds: float = 3.0,
                 settle_seconds: float = 5.0, logger=None, on_idle=None,
                 away_idle_seconds: int = 600,
                 focus_idle_seconds: int = 7200):
        self.on_change = on_change
        self.on_idle = on_idle
        self.poll_seconds = poll_seconds
        self.settle_seconds = settle_seconds
        self.log = logger or (lambda *a, **k: None)
        # 空闲多久算「离开」。默认和 config.AWAY_IDLE_SECONDS 一致；
        # 由调用方传入而不是这里 import config —— 这个模块刻意不依赖 config，
        # 免得和 collector 之间形成循环导入。
        self.away_idle_seconds = int(away_idle_seconds)
        # 专注模式下的阈值。开关由采集器控制（它持有一个线程安全的状态）。
        self.focus_idle_seconds = int(focus_idle_seconds)
        self.is_focus = None        # 可调用的判定函数，由采集器注入

        # 这几个实例变量必须留在 __init__ 里 —— 之前一次编辑把它们挪到了
        # 下面那个 property 的 return 之后，成了永远执行不到的死代码，
        # 结果采集线程一启动就 AttributeError: '_stop'（实测踩过）。
        self._stop = False
        self._pending_sig = None
        self._pending_since = 0.0
        self._last_sig = None
        self._last_proc = ""
        self._last_title = ""
        self._last_cls = ""

    @property
    def effective_away_seconds(self) -> int:
        """当前生效的离开阈值。"""
        try:
            if self.is_focus and self.is_focus():
                return self.focus_idle_seconds
        except Exception:
            pass
        return self.away_idle_seconds

    def stop(self):
        self._stop = True

    def current(self) -> tuple[str, str, int, str]:
        return foreground_window()

    def tick(self) -> tuple[str, str, str, str] | None:
        """
        走一拍。真的换了窗口就返回 (状态, 进程名, 标题, 窗口类)，
        状态是 "settled"。否则返回 None。
        """
        title, proc, pid, cls = foreground_window()
        sig = window_signature(proc, title, cls)

        if not sig.strip("|"):
            return None
        if should_ignore(proc, title, cls):
            return None

        now = time.time()
        result = None

        if sig != self._last_sig:
            # 还没定下来：看看跟上一拍待定的是不是同一个
            if sig == self._pending_sig:
                if now - self._pending_since >= self.settle_seconds:
                    self._last_sig = sig
                    self._last_proc = proc
                    self._last_title = title
                    self._last_cls = cls
                    self._pending_sig = None
                    result = ("settled", proc, title, cls)
            else:
                self._pending_sig = sig
                self._pending_since = now
        else:
            # 一直是同一个窗口
            self._pending_sig = None

        return result

    def run(self):
        last_idle_log = 0.0
        while not self._stop:
            try:
                ev = self.tick()
                if ev:
                    kind, proc, title, cls = ev
                    self.on_change(proc, title, cls)

                # 定期记一条「键鼠多久没动」。这是判断「你是不是离开电脑」
                # 的唯一可靠依据 —— 靠事件间隔猜会把你专心写代码的一小时
                # 当成离开电脑。
                now = time.time()
                if now - last_idle_log >= IDLE_SAMPLE_SECONDS:
                    last_idle_log = now
                    if self.on_idle:
                        self.on_idle(idle_seconds())
            except Exception as e:
                self.log(f"[监测] 异常: {e}")
            # 用 wait 而不是 sleep，退出时能立刻响应
            deadline = time.time() + self.poll_seconds
            while not self._stop and time.time() < deadline:
                time.sleep(0.2)

    def prime(self):
        """
        启动时处理「当前已经在用的那个窗口」。

        这里有两个必须小心的场景，都是实测踩过的：

        **一、启动前就在看文献，之后一直没切窗口**
            如果只是把当前窗口记成「基线」（_last_sig），它永远不会变成
            一条事件 —— 而事件只在「窗口变化」时产生。结果这段时间完全
            不被记录，日记里那几小时是空的。
            所以这里把它当成一条**待记录的窗口**：谁先来谁触发落盘
            （切窗口、或者程序退出）。正常开始用电脑时很快就会切一次。

        **二、启动时你其实已经走开了**
            比如你早上开程序、人却还没坐下（键鼠空闲已经 300 秒）。
            上一版无条件把当前窗口存成待定窗口，于是晚上退出时会落一条
            事件 —— 一个你根本没碰电脑的日子，却凭空多出一个数据文件。
            所以先看空闲时长：已经算「离开」的就先不记，等你真动了键鼠，
            tick() 会正常产生窗口事件。
        """
        title, proc, pid, cls = foreground_window()
        self._last_sig = window_signature(proc, title, cls)
        self._last_proc = proc
        self._last_title = title
        self._last_cls = cls

        # 桌面 / 被忽略的窗口不当成活动，否则一开机就记一条「桌面」
        if should_ignore(proc, title, cls):
            return
        # 启动那一刻就明显空闲：说明你人不在，别把眼下这个窗口算成活动。
        #
        # 阈值用 120 秒而不是「离开」那 600 秒 —— 这里判断的是**启动瞬间**
        # 你人到底在不在，标准要比「中途离开」严格得多：
        # 用 600 秒的话，你早上开程序时只是 5 分钟没动键鼠，就会被记成
        # 「你正在用这个窗口」，于是晚上退出时凭空写出一条事件和数据文件。
        # 120 秒足够把「刚动过键鼠」和「其实没在」分开。
        try:
            if idle_seconds() >= PRIME_IDLE_LIMIT:
                self.log(f"[监测] 启动时已空闲 {idle_seconds()} 秒，"
                         f"暂不把当前窗口算作活动")
                return
        except Exception:
            pass
        # 交给外面处理（收集器会把它存成待定窗口，等下一次落盘时机）
        if self.on_change:
            self.on_change(proc, title, cls)
