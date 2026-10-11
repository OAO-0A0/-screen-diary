"""
屏幕日记 —— 配置

所有可调参数集中在这里。用户改这个文件就能调整行为，不用碰主程序。
计划书存成 JSON，GUI 里也能改。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# 数据目录和配置文件都可以用环境变量重定向。
#
# 为什么需要：回归测试要造很多天的假事件，还要反复改「计划」。如果它们
# 和真实数据共用 data/ 与 config.json，跑一次测试就会把你的演示数据、
# 真日记、**以及你设的计划**全污染掉。实测反复踩过 —— 每次跑完测试都要
# 手工挑出哪些文件是测试留下的，有一次还因此把演示数据删了。
#
# 测试只要在导入本模块前设置 SCREENJOURNAL_DATA 指向临时目录，
# 就完全不会碰你的数据和配置。
# 注意 config.json 也要一起重定向：计划就存在它里面，光隔离 data/ 不够
# （实测就是这样丢掉过一整周的演示计划）。
_override = os.environ.get("SCREENJOURNAL_DATA", "").strip()
_ov = Path(_override) if _override else None

DATA_DIR = (_ov / "data") if _ov else (BASE_DIR / "data")
SHOT_DIR = DATA_DIR / "shots"            # 截图（当天有效，分析后删）
DIARY_DIR = (_ov / "diary") if _ov else (BASE_DIR / "diary")
CONFIG_PATH = (_ov / "config.json") if _ov else (BASE_DIR / "config.json")

OLLAMA_API = "http://127.0.0.1:11434"

# ---------------- 采集 ----------------
POLL_SECONDS = 3          # 多久看一眼前台窗口
SETTLE_SECONDS = 5        # 窗口切换后稳定多久才截图（防止 Alt+Tab 连拍）
SHOT_MAX_WIDTH = 896      # 截图缩放宽度（视觉模型内部只吃这么多，再大纯浪费）
SHOT_QUALITY = 75         # JPEG 质量
# 键鼠空闲超过这么久，就判定为你离开电脑了。
# 采集端和统计端必须用同一个值，所以放在这里。
AWAY_IDLE_SECONDS = 10 * 60

# 专注模式下的离开阈值。
#
# 为什么需要这个模式：看网课、思考难题时你可能几十分钟不碰键鼠，
# 但人明明在电脑前。用默认的 10 分钟阈值，实测「看 40 分钟网课」
# 会被记成 1 分钟的活跃 —— 那段时间既不算活跃也不算离开，直接悬空。
#
# 拉长到 2 小时：中途起身倒水、走神都不会被误判成离开。
# 代价是这段时间里如果你真走了，也会算在用 —— 所以它是个**手动开关**，
# 由你自己判断「接下来这段时间我会一直在这儿」。
FOCUS_IDLE_SECONDS = 2 * 60 * 60

# 专注模式多久后自动关掉。和上面的离开阈值用同一个值。
#
# 为什么要自动关：这个模式是「接下来这段时间我会一直在这儿」的承诺。
# 忘了关的话它会一直生效下去，你后面真走开的时间全被算成活跃，
# 那天的时长就虚高了。两小时后自动回落，风险窗口有界。
FOCUS_AUTO_OFF_SECONDS = FOCUS_IDLE_SECONDS

# ---------------- 视觉分析 ----------------
#
# 两个模型都用**官方名字**，这样 `ollama pull <名字>` 就能装上，
# 不需要额外的 Modelfile 导入步骤：
#
#     ollama pull gemma3:4b     视觉模型（读截图）
#     ollama pull qwen3:4b      文本模型（写总结）
#
# gemma3 是唯一在 4B 这个量级带视觉能力的常用模型。
#
# 想换更小的省显存，或换成自己导入的模型，改这两行即可。
# 注意：那个模型必须真的支持 vision（`ollama show <名字>` 看 capabilities），
# 否则读图会失败 —— 实测 gemma3:1b 就没有 vision，只有 completion。
VISION_MODEL = "gemma3:4b"
TEXT_MODEL = "qwen3:4b"
VISION_IMAGE_SECONDS = 1.2     # 实测每张约 1.1 秒，用来估算进度
MAX_SHOTS_PER_ANALYSIS = 800   # 一天最多分析多少张，防止失控

# ---------------- 保留策略 ----------------
# 截图保留几天。分析成功后当天的图会立刻删掉（这是主路径），
# 这个值是给「忘了分析就关机」兜底的 —— 留几天让你还能补分析。
#
# 设成 7 是因为现在有周报了：一周之内你随时可以补分析某天，
# 补完后周报也能跟着重算。超过 7 天的截图目录会在启动时清掉。
KEEP_SHOTS_DAYS = 7

# ---------------- 默认配置 ----------------
DEFAULT_CONFIG = {
    "poll_seconds": POLL_SECONDS,
    "settle_seconds": SETTLE_SECONDS,
    "shot_max_width": SHOT_MAX_WIDTH,
    "keep_shots_days": KEEP_SHOTS_DAYS,
    "focus_mode": False,      # 专注模式：把离开阈值拉到 FOCUS_IDLE_SECONDS
    "plan": [
        {"start": "09:00", "end": "11:00", "task": "写代码"},
        {"start": "14:00", "end": "16:00", "task": "看论文"},
    ],
    "plan_done": {},          # {"写代码": true} 由界面里打勾写入
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))     # 深拷贝
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                cfg.update(saved)
        except Exception:
            pass          # 配置坏了就用默认，不让程序起不来
    return cfg


def save_config(cfg: dict) -> bool:
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def ensure_dirs():
    for d in (DATA_DIR, SHOT_DIR, DIARY_DIR):
        d.mkdir(parents=True, exist_ok=True)


# 注意：这里曾经有一份 IGNORE_PROCESSES（按进程名排除）。
# 那份名单把 explorer.exe / systemsettings.exe / applicationframehost.exe
# 一刀砍掉，导致文件资源管理器、设置这些系统自带应用全都识别不出来。
# 已改成在 activity.py 里按「窗口类 + 有没有标题」判断，名单已废弃。

# 进程名 -> 好看的中文/英文显示名（没命中的就用进程名）
PRETTY_NAMES = {
    "code.exe": "VS Code",
    "code - insiders.exe": "VS Code Insiders",
    "pycharm64.exe": "PyCharm",
    "devenv.exe": "Visual Studio",
    "idea64.exe": "IntelliJ IDEA",
    "goland64.exe": "GoLand",
    "webstorm64.exe": "WebStorm",
    "sublime_text.exe": "Sublime Text",
    "notepad++.exe": "Notepad++",
    "notepad.exe": "记事本",
    "winword.exe": "Word",
    "excel.exe": "Excel",
    "powerpnt.exe": "PowerPoint",
    "outlook.exe": "Outlook",
    "chrome.exe": "Chrome",
    "msedge.exe": "Edge",
    "firefox.exe": "Firefox",
    "brave.exe": "Brave",
    "opera.exe": "Opera",
    "wechat.exe": "微信",
    "weixin.exe": "微信",
    "qq.exe": "QQ",
    "dingtalk.exe": "钉钉",
    "feishu.exe": "飞书",
    "telegram.exe": "Telegram",
    "discord.exe": "Discord",
    "slack.exe": "Slack",
    "windowsterminal.exe": "Windows Terminal",
    "cmd.exe": "命令提示符",
    "powershell.exe": "PowerShell",
    "pwsh.exe": "PowerShell 7",
    "explorer.exe": "文件资源管理器",
    "wps.exe": "WPS",
    "et.exe": "WPS 表格",
    "wpp.exe": "WPS 演示",
    "acrobat.exe": "Acrobat",
    "acrord32.exe": "Acrobat Reader",
    "sumatrapdf.exe": "SumatraPDF",
    "foxitreader.exe": "Foxit Reader",
    "adb.exe": "ADB",
    "python.exe": "Python",
    "pythonw.exe": "Python",
    "java.exe": "Java",
    "node.exe": "Node.js",
    "ollama app.exe": "Ollama",
    "obs64.exe": "OBS",
    "photoshop.exe": "Photoshop",
    "illustrator.exe": "Illustrator",
    "figma.exe": "Figma",
    "blender.exe": "Blender",
    "steam.exe": "Steam",
    "steamwebhelper.exe": "Steam",
    "bilibili.exe": "哔哩哔哩",
    "cloudmusic.exe": "网易云音乐",
    "qqmusic.exe": "QQ音乐",
    "potplayermini64.exe": "PotPlayer",
    "vlc.exe": "VLC",
    "mpv.exe": "mpv",
    # 系统自带应用
    "taskmgr.exe": "任务管理器",
    "systemsettings.exe": "设置",
    "applicationframehost.exe": "系统应用",   # 空标题时才会走到这，正常会被标题覆盖
    "calculator.exe": "计算器",
    "windowscamera.exe": "相机",
    "snippingtool.exe": "截图工具",
    "mspaint.exe": "画图",
    "notepad.exe": "记事本",
    "control.exe": "控制面板",
    "mmc.exe": "管理控制台",
    "regedit.exe": "注册表编辑器",
    "photos.exe": "照片",
}


# UWP / 系统应用的标题 -> 显示名。
# 这些应用的前台进程都叫 applicationframehost.exe，只能靠标题认。
# 按长度从长到短匹配，免得「设置」把「设置助手」这种吃掉。
UWP_TITLE_NAMES = {
    "文件资源管理器": "文件资源管理器",
    "file explorer": "文件资源管理器",
    "任务管理器": "任务管理器",
    "task manager": "任务管理器",
    "设置": "设置",
    "settings": "设置",
    "计算器": "计算器",
    "calculator": "计算器",
    "照片": "照片",
    "photos": "照片",
    "相机": "相机",
    "camera": "相机",
    "画图": "画图",
    "paint": "画图",
    "记事本": "记事本",
    "notepad": "记事本",
    "截图工具": "截图工具",
    "snipping tool": "截图工具",
    "应用商店": "应用商店",
    "microsoft store": "应用商店",
    "时钟": "时钟",
    "clock": "时钟",
    "日历": "日历",
    "calendar": "日历",
    "地图": "地图",
    "maps": "地图",
    "天气": "天气",
    "weather": "天气",
    "邮件": "邮件",
    "mail": "邮件",
    "终端": "Windows 终端",
    "terminal": "Windows 终端",
    "控制面板": "控制面板",
    "control panel": "控制面板",
}

# 这些进程名不好看，用更有信息量的：文件管理器显示成「文件夹」更贴切
PROC_OVERRIDES = {
    "explorer.exe": "文件资源管理器",
}


def pretty_name(proc: str, title: str = "") -> str:
    """
    把进程名变成人看得懂的名字。

    顺序有讲究：
      1. 标题能认出来是哪个系统应用 -> 用那个（UWP 全靠这个）
      2. 进程名有映射 -> 用映射
      3. 浏览器标题里的站点名 -> 用站点名
      4. 都不中 -> 用进程名
    """
    p = (proc or "").lower()
    low = (title or "").lower()

    # 1) 标题优先：applicationframehost.exe 承载的 UWP 应用只能这样认
    for key, name in sorted(UWP_TITLE_NAMES.items(), key=lambda kv: -len(kv[0])):
        if key in low:
            return name

    # 1b) UWP 外框进程：标题没命中映射表时，直接把标题当应用名。
    #     比笼统显示成「系统应用」有用得多。
    if p in ("applicationframehost.exe", "systemsettings.exe") and title.strip():
        return title.strip()[:20]

    # 2) 进程名映射
    if p in PROC_OVERRIDES:
        return PROC_OVERRIDES[p]
    if p in PRETTY_NAMES:
        return PRETTY_NAMES[p]

    if not p:
        return "未知"

    # 3) 浏览器里打开的站点，用域名当名字更有信息量
    for site, name in (
        ("bilibili", "哔哩哔哩"), ("youtube", "YouTube"),
        ("github", "GitHub"), ("zhihu", "知乎"),
        ("stackoverflow", "Stack Overflow"), ("chatgpt", "ChatGPT"),
        ("deepseek", "DeepSeek"),
    ):
        if site in low:
            return name

    # 4) 兜底
    return p[:-4] if p.endswith(".exe") else p
