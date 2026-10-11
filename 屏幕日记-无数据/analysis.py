"""
屏幕日记 —— 统计

时长算法是这里唯一容易做错的地方，说明白：

  按「事件之间的时间差」累加，不是「数几张截图」。
      09:12:03 VS Code
      09:41:17 Chrome     -> VS Code 记 +29分14秒
      10:03:55 VS Code    -> Chrome 记 +22分38秒

  两个概念必须分开，混在一起就会算错（我第一版就混了）：
    * **窗口停留太久** —— 你盯着同一个窗口 40 分钟，那是真的在用，
      必须照实算。用 MAX_WINDOW_SECONDS 封顶只是防止「忘关的窗口」无限累加。
      实测：真实工作时窗口停留常在 20~40 分钟，封太小会把时长全吃掉。
    * **真的离开电脑** —— 键盘鼠标长时间没动。这个靠采集时的 idle 事件判断，
      不能靠「事件间隔大」猜，否则你专心写代码一小时会被当成离开。

  最后一段（还没有下一个事件）单独封顶，否则你睡了程序还在累加。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from config import pretty_name

# 单个窗口最长按这么久算。
#
# 这个值的沿革值得说清楚，因为设小了的代价很大：
#   最早设成 90 分钟，本意是防「忘了关的窗口」无限刷时长。但那类问题
#   已经由另外两件事解决了 —— 采集端排除自己（pythonw 不再记成你的活动），
#   以及键鼠空闲检测（真走了会写成离开区间）。
#   于是这个封顶只剩下副作用：连续看 2 小时文献、连续写 3 小时代码，
#   时长会被硬生生砍掉。实测过：3 小时的阅读只算出 1.5 小时。
#
# 现在它只作为「病态数据」的安全上限存在（比如系统时间被改），
# 正常使用不会碰到。
MAX_WINDOW_SECONDS = 4 * 60 * 60
# 最后一段（没有后续事件）最多多算这么久，覆盖「最后一次活动到退出」
MAX_TAIL_SECONDS = 5 * 60
# 有 idle 事件时，空闲超过这么久才算离开
AWAY_IDLE_SECONDS = 10 * 60


def _parse(ts: str) -> float | None:
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return None


def _focus_spans(events: list[dict]) -> list[tuple[float, float]]:
    """
    找出「专注模式」覆盖的时间段。

    专注模式期间不判离开（你不会碰键鼠也可能是在看网课），所以那段时间
    一律算活跃。实现上：采集端会在专注期间写的事件上打 `focus: True`，
    这里把它们连成区间，然后**丢掉落在专注时段里的离开区间**。

    为什么要有这一步：专注开着时，你中途起身 30 分钟仍然会被记成一段
    离开（阈值是 2 小时，但回来时还是会写一条区间）。那段时间按约定
    算活跃，所以要把那条区间丢掉，否则会被扣掉。

    连片的判定：两个相邻 focus 事件之间不超过 FOCUS_JOIN_GAP 就视为
    同一段。心跳间隔最多 180 秒，取 10 分钟足够连上，又不会把两段
    相隔很久的专注粘成一段。
    """
    FOCUS_JOIN_GAP = 600
    ts_list = sorted(float(e.get("ts") or 0) for e in events
                     if e.get("focus") and e.get("ts"))
    if not ts_list:
        return []
    spans: list[list[float]] = [[ts_list[0], ts_list[0]]]
    for t in ts_list[1:]:
        if t - spans[-1][1] <= FOCUS_JOIN_GAP:
            spans[-1][1] = t
        else:
            spans.append([t, t])
    return [(a, b) for a, b in spans]


def _away_intervals(events: list[dict]) -> list[tuple[float, float]]:
    """
    找出「你确实离开了电脑」的时间区间。

    离开起点怎么定 —— 这一步很容易做错，说明白：

      采集端记录的空闲时长是「从你最后一次动键鼠算起」。所以
      「离开起点」有两个候选：

        * 空闲推算：ts - idle       （你最后一次动键鼠的时刻）
        * 上一个窗口事件开始的时刻   （你最后一次操作的样子）

      必须取**更早的那个**。否则会有时间凭空消失：
      比如你 10:00 切到浏览器、10:02 就不动了，程序 10:12 才判定离开，
      那么 10:00–10:02 这两分钟既不算给浏览器（被截断在离开起点），
      也不算进离开 —— 谁都没拿到。

      这个问题在采集端已经修过一次（见 collector._on_idle 里的 by_window），
      但分析端当时漏改了，导致统计对不上（实测用例 7 少了 100 秒）。
      两处必须保持一致。
    """
    # 窗口事件时刻，用来当「离开起点」的另一个候选
    win_ts = sorted(float(e.get("ts") or 0) for e in events
                    if e.get("kind") == "window" and e.get("ts"))

    def last_win_before(t: float) -> float | None:
        prev = None
        for w in win_ts:
            if w < t:
                prev = w
            else:
                break
        return prev

    out = []
    for e in events:
        if e.get("kind") != "idle":
            continue
        ts = float(e.get("ts") or 0)
        if not ts:
            continue
        start = float(e.get("start") or 0)
        end = float(e.get("end") or 0)
        # 写这条区间时生效的阈值。专注模式（2 小时）下写出的 30 分钟区间
        # 在专注期间是成立的，但不能拿默认的 10 分钟去判它 —— 那会把
        # 专注期间的小停顿扣成离开。采集端会把这个值一起写进事件。
        thr = float(e.get("threshold") or AWAY_IDLE_SECONDS)
        if not (start and end and end > start):
            idle = float(e.get("idle") or 0)
            if idle < thr:
                continue
            start, end = max(0.0, ts - idle), ts

        # 起点必须晚于「区间终点之前的最后一个窗口事件」。
        #
        # 为什么用**终点**去找而不是起点：空闲回推点可能早得离谱
        # （采集端旧实现取 min 时，实测写出过比最后一次操作早 540 秒的区间）。
        # 如果只拿起点去比，会漏掉「起点之后、终点之前」那段真实操作，
        # 于是整段在用时间被吞成离开 —— 实测吞掉过一段 30 分钟的阅读。
        # 用终点去找，才能发现「离开区间里其实还穿插着窗口切换」这种矛盾数据。
        last = last_win_before(end)
        if last is not None and last > start:
            start = last
        if end > start:
            out.append((start, end))

    # 合并重叠/相邻的区间，避免同一段被减两次
    out.sort()
    merged: list[list[float]] = []
    for a, b in out:
        if merged and a <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def _overlap(a1: float, a2: float, b1: float, b2: float) -> float:
    return max(0.0, min(a2, b2) - max(a1, b1))


def compute(events: list[dict]) -> dict:
    """
    从事件流算出：每个软件多久、总活跃时长、离开时长、时间线。
    """
    wins = [e for e in events if e.get("kind") == "window"]
    wins.sort(key=lambda e: e.get("ts", 0))
    away = _away_intervals(events)

    # 专注模式覆盖的时段一律算活跃：把落在里面的离开区间丢掉。
    # （专注开着时你中途起身仍然会被记一条离开，但按约定那段时间算在用。）
    focus_spans = _focus_spans(events)
    if focus_spans:
        kept = []
        for a, b in away:
            drop = False
            for fa, fb in focus_spans:
                # 区间中点落在专注时段内就丢掉整条
                mid = (a + b) / 2
                if fa <= mid <= fb:
                    drop = True
                    break
            if not drop:
                kept.append((a, b))
        away = kept

    # 离开区间的终点也要被「回来后的第一个窗口」截住：
    # 有些 idle 区间会一路延伸到采集结束，不收尾的话会把它算得过长。
    #
    # 注意 awake 标记的位置：采集端写的是顶层 awake（因为 append_event 会把
    # extra 的键合并到顶层）。这里两种位置都读，免得又因为位置不一致而失效
    # —— 之前只读 extra["awake"]，结果这条截断从来没生效过。
    def _is_awake(e: dict) -> bool:
        if e.get("awake"):
            return True
        extra = e.get("extra")
        return bool(isinstance(extra, dict) and extra.get("awake"))

    boundaries = sorted(float(e.get("ts") or 0) for e in wins
                        if _is_awake(e) and e.get("ts"))
    fixed_away: list[tuple[float, float]] = []
    for a, b in away:
        nxt = next((t for t in boundaries if t > a), None)
        fixed_away.append((a, min(b, nxt) if nxt else b))
    away = fixed_away

    app_seconds: dict[str, float] = defaultdict(float)
    app_shots: dict[str, int] = defaultdict(int)
    timeline: list[dict] = []
    away_seconds = 0.0
    total_active = 0.0

    # 最后一条窗口事件的结束时刻。
    # 优先看「最后一次实际活动」在哪：如果键鼠活动一直延续到很晚，
    # 就没有理由把它截短（否则安静读一小时 PDF 会被压成 5 分钟）。
    # 活动证据取三者里最晚的：最后一张截图、最后一条 idle 采样、下一条事件。
    last_activity = 0.0
    for e in wins:
        if e.get("shot") and e.get("ts"):
            last_activity = max(last_activity, float(e["ts"]))
    for e in events:
        if e.get("kind") == "idle" and e.get("ts"):
            ent = float(e.get("end") or 0) or float(e["ts"])
            last_activity = max(last_activity, ent)
    # 最后一段的合法时长 = 从它开始到「最后活动 + 一点余量」
    tail_end = (last_activity + 120) if last_activity else 0.0

    for i, e in enumerate(wins):
        # 用 is None 而不是 not ts：0 是合法的（虽然真实时间戳不会为 0，
        # 但把 0 当无效值会让测试和补录数据踩坑）。
        ts_raw = e.get("ts")
        if ts_raw is None:
            continue
        try:
            start = float(ts_raw)
        except (TypeError, ValueError):
            continue
        proc = e.get("proc", "")
        title = e.get("title", "")
        name = pretty_name(proc, title)

        if i + 1 < len(wins):
            end = float(wins[i + 1].get("ts", start) or start)
            # 注意：这里**不**做「自己没截图就截断到下一段截图时刻」的处理。
            #
            # 曾经加过这条规则，用来修「08:52 随手看一眼天气（没截图），
            # 09:05 才开始正事，那 13 分钟全算给了天气」。
            # 但它会同时误伤真正的情况：安静读 30 分钟 PDF（没截图、也不产生
            # idle 采样），下一段一有截图就被砍成几十秒 —— 时长凭空蒸发。
            #
            # 两种情况对这个规则的要求正好相反，而「没截图」这个信号无法
            # 区分它们（可能是窗口没停够 5 秒的过渡，也可能是长时间专心使用）。
            # 与其猜错，不如保留完整时长：虚高几分钟，好过把真实工作时长吃掉。
        else:
            # 最后一段没有后续事件。
            #
            # 以前是「按最后一张截图 +60 秒封顶」，这有个要命的副作用：
            # 如果这段时间根本没截图（比如你安静读了一小时论文），
            # 就会退化成只算 60 秒，时长凭空蒸发。
            #
            # 改成看「活动证据」：
            #   有证据（截图 / 键鼠心跳） -> 一直算到证据那一刻再加点余量
            #   完全没有证据             -> 才退回固定上限
            #
            # 注意别在这里再套 MAX_WINDOW_SECONDS：
            # 证据指向 3 小时，套上 4 小时上限没问题；但如果证据刚好超过上限
            # 就会掉进兜底分支，整段变成 60 秒 —— 实测 3 小时阅读只剩 7 分钟。
            if tail_end > start:
                end = tail_end
            else:
                end = start + 60

        # 先按窗口停留封顶（防止忘了关的窗口刷时长）
        end = min(end, start + MAX_WINDOW_SECONDS)

        # 如果这句之后就是你离开的起点，窗口时长到此为止 ——
        # 这是「回到桌面只剩日记软件」那个问题的正解。
        # 取所有重叠区间里最早的那个起点，避免受区间顺序影响。
        #
        # 只往前截断（end = min(end, a1) 仅当 a1 >= start）。
        # 如果 a1 < start（回顾算出的离开起点比这条窗口还早），
        # 说明这条窗口整个落在离开时段里 —— 那种情况交给下面的重叠扣减，
        # 会把它的可用时长扣成 0。早先这里直接 min 会导致负数（实测踩过）。
        for (a1, a2) in away:
            if a1 <= start < a2:
                end = min(end, a1)
            elif start < a1 < end:
                end = min(end, a1)

        span = max(0.0, end - start)

        # 再扣掉残留的离开时段（重叠部分）
        away_here = 0.0
        for (a1, a2) in away:
            away_here += _overlap(start, end, a1, a2)
        away_here = min(away_here, span)
        used = span - away_here
        away_seconds += away_here

        app_seconds[name] += used
        if e.get("shot"):
            app_shots[name] += 1
        total_active += used

        timeline.append({
            "t": e.get("t", ""),
            "app": name,
            "title": title,
            "proc": proc,
            "seconds": round(used),
            "shot": e.get("shot", ""),
        })

    # 离开总时长：以区间为准，别再叠加（否则会重复计算）
    away_total = sum(b - a for a, b in away)
    # 但如果离开区间只覆盖了一部分，剩下的仍然按上面扣掉的算
    away_seconds = max(away_seconds, 0.0)
    if away_total > 0:
        away_seconds = away_total

    ranked = sorted(app_seconds.items(), key=lambda kv: -kv[1])
    apps = [{
        "name": n,
        "seconds": round(s),
        "minutes": round(s / 60, 1),
        "share": round(s / total_active, 4) if total_active else 0,
        "shots": app_shots.get(n, 0),
    } for n, s in ranked if s > 0]

    return {
        "apps": apps,
        "total_active_seconds": round(total_active),
        "away_seconds": round(away_seconds),
        "window_events": len(wins),
        "shots": sum(1 for e in wins if e.get("shot")),
        "timeline": timeline,
        "first_event": wins[0].get("t", "") if wins else "",
        "last_event": wins[-1].get("t", "") if wins else "",
    }


def plan_review(stats: dict, plan: list[dict], plan_done: dict,
                sessions: list[dict]) -> dict:
    """
    把计划和实际对一下。

    完成与否以「你打的勾」为准 —— 不让模型猜。原因：在 VS Code 待了 90 分钟
    不等于写了 90 分钟代码，模型推断这个只会给你一个看着确定、其实瞎猜的数字。

    但「参考时长」要算得靠谱，所以按**计划时段与窗口事件的时间重叠**来算，
    而不是按软件名匹配。软件名匹配会重复计算：你看论文时开着 Chrome 查词，
    Chrome 的时间就会被同时算进「看论文」和「查资料」。
    """
    rows = []
    hits = 0
    counted = 0
    timeline = stats.get("timeline", [])

    for item in plan or []:
        task = (item.get("task") or "").strip()
        if not task:
            continue
        start, end = item.get("start", ""), item.get("end", "")
        want_min = _minutes_between(start, end)
        actual = _task_seconds_overlap(stats, timeline, start, end, task)
        done = plan_done.get(task)

        counted += 1
        if done is True:
            hits += 1

        rows.append({
            "task": task,
            "start": start,
            "end": end,
            "planned_minutes": want_min,
            "actual_minutes": round(actual / 60, 1),
            "done": done,                    # True / False / None(没打勾)
        })

    # 手动段也算进「计划完成」的参考
    manual = [{
        "task": s.get("task") or "（未命名）",
        "minutes": round((s.get("seconds") or 0) / 60, 1),
        "start": (s.get("start") or "")[-8:],
        "end": (s.get("end") or "")[-8:],
    } for s in sessions if s.get("end")]

    rate = (hits / counted) if counted else 0.0
    return {"rows": rows, "done": hits, "total": counted,
            "rate": round(rate, 3), "manual_sessions": manual}


def _minutes_between(start: str, end: str) -> int:
    try:
        h1, m1 = (int(x) for x in start.split(":"))
        h2, m2 = (int(x) for x in end.split(":"))
        a, b = h1 * 60 + m1, h2 * 60 + m2
        return max(0, b - a) if b >= a else max(0, 1440 - a + b)
    except Exception:
        return 0


# 计划里的活对应哪些软件 —— 用来在「时段重叠」的基础上做加权。
# 只是提高相关度判断，不做硬过滤：看你论文时顺手查个词也该算进去。
TASK_APP_HINTS = {
    "写代码": ["VS Code", "PyCharm", "Visual Studio", "IntelliJ IDEA", "GoLand",
               "WebStorm", "Sublime Text", "Notepad++", "Windows Terminal",
               "PowerShell", "命令提示符", "Python", "Node.js", "Java"],
    "看论文": ["Acrobat", "Acrobat Reader", "SumatraPDF", "Foxit Reader", "Zotero"],
    "看书": ["Acrobat", "Acrobat Reader", "SumatraPDF"],
    "写文档": ["Word", "WPS", "Typora", "Notion"],
    "学习": ["Acrobat", "SumatraPDF", "哔哩哔哩"],
    "开会": ["腾讯会议", "Zoom", "飞书", "钉钉", "Teams"],
    "打游戏": [],
}

# 浏览器：几乎什么任务都可能用到，所以只按重叠算、不额外加权
NEUTRAL_APPS = {"Chrome", "Edge", "Firefox", "Brave", "Opera", "Safari"}


def _task_seconds_overlap(stats: dict, timeline: list[dict],
                          start: str, end: str, task: str) -> float:
    """
    算计划时段里「相关活动」占了多少秒。

    做法：把每个窗口事件的时间区间和计划时段求交集，
    再按该软件和任务的匹配度加权（完全匹配算满，浏览器算部分）。
    这样就不会出现「同一次浏览器停留被两个任务各记一遍」。
    """
    if not timeline:
        return 0.0

    # 计划时段换算成当天的时间点
    t0 = _day_clock(stats, start, "start")
    t1 = _day_clock(stats, end, "end")
    if t1 <= t0:
        return 0.0

    # 相关软件集合
    related: set[str] = set()
    for key, apps in TASK_APP_HINTS.items():
        if key in task or task in key:
            related.update(apps)
            break

    total = 0.0
    for w in timeline:
        ts = _parse_time_of_day(w.get("t", ""))
        if ts is None:
            continue
        a2 = ts + max(1.0, float(w.get("seconds", 0) or 0))
        ov = max(0.0, min(a2, t1) - max(ts, t0))
        if ov <= 0:
            continue
        app = w.get("app", "")
        if app in related:
            total += ov
        elif app in NEUTRAL_APPS:
            total += ov * 0.5      # 浏览器：可能是在做这事，也可能不是
        # 其它软件不计入
    return total


def _parse_time_of_day(clock: str) -> float | None:
    """把 "2026-10-05 09:15:00" 或 "09:15:00" 转成当天的秒数。"""
    if not clock:
        return None
    s = clock[-8:]
    try:
        h, m, sec = (int(x) for x in s.split(":"))
        return h * 3600 + m * 60 + sec
    except Exception:
        return None


def _day_clock(stats: dict, hhmm: str, which: str) -> float:
    """把计划的 "09:00" 转成当天秒数。"""
    try:
        h, m = (int(x) for x in hhmm.split(":"))
    except Exception:
        return 0.0
    return h * 3600 + m * 60


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}小时{m}分"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"
