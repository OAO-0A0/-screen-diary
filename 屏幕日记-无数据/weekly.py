"""
屏幕日记 —— 周报

和日报的关系：日报是「那天屏幕上都发生了什么」，周报只回答一个问题 ——
**这一周的时间去哪了**。所以周报刻意不生成「屏幕印象」那种逐张截图的描述，
也不列时间线。一天几十张图的印象堆到一周就是几百条，读的人一条都不会看，
只会把真正有用的东西（几天在记、最忙哪天、时间花在哪个软件上）淹掉。

根据 store.py 的新约定（目录按「月 / 周 / 天」三级），周报和它那张图放在
**周目录**里：

    diary/2026-10/2026-W41/
      ├── 2026-10-05.md            每日日记
      ├── weekly-2026-W41.md       周报
      └── weekly-chart-2026-W41.png 周图

路径不在这里拼，一律走 `Store.diary_dir()` / `Store.weekly_path()` —— 目录
规则只有 store.py 一个地方知道。为兼容旧版 store，`_week_dir()` 和
`run_weekly()` 里各留了一处 `getattr` 兜底，store 更新到哪一版都能跑。

计划完成情况优先读**当天的计划快照**（summary-<day>.json 里的 plan/plan_done），
拿不到才退回当前的 config.json。这样补写历史周报时，看到的才是那天真实的计划，
而不是今天改过的计划。

日期一律交给标准库算（`date.isocalendar()`），不自己写周数和跨年逻辑 ——
跨年那几天的归属（比如 2029-12-31 属于 2030 年第 1 周）自己推必错。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from analysis import compute, fmt_duration, plan_review
from charts import BG, FG, GRID, MUTED, PALETTE, _font
from config import load_config
from store import Store
import diary

# 图例最多列几个软件，剩下的归成「其它」。
#
# 6 是个折中：太少则一个偏杂的星期里最大那块变成「其它」，看不出你主要在用什么；
# 太多则图例比图本身还长，而周报的图只需要「一眼看出分布」。
LEGEND_TOP = 6

# Markdown 里的软件表列几行。日报是 15 行，周报压到 8 行。
MAX_APPS_ROWS = 8

WEEKDAY_CN = "一二三四五六日"          # date.weekday(): 周一 = 0

WEEK_TITLE = "屏幕日记 · 周报"

# 「其它」那一段和它的图例色。用中性灰而不是 PALETTE 里的彩色：
# 它不是一个软件，给彩色会让人以为榜单上真有这么个应用；
# 顺带也避开「图例第 7 项正好和 PALETTE[6] 撞色」。
#
# 选深一点的灰（而不是更浅的 (150,156,166)）纯粹是可读性：空条底条是
# GRID(228,232,240)，浅灰的小段紧挨着它时不够清楚。两种都能用，这个更稳。
OTHER_LABEL = "其它"
OTHER_COLOR = (96, 102, 112)


# ---------------------------------------------------------------
#  周（ISO 周，周一为一周开始）
# ---------------------------------------------------------------
def week_key(day: str) -> str:
    """ "2026-10-05" -> "2026-W41"。跨年由 isocalendar() 决定归属。"""
    y, w, _ = date.fromisoformat(day).isocalendar()
    return f"{y}-W{w:02d}"


def week_days(day: str) -> list[str]:
    """该日期所在周的 7 天（周一..周日）。"""
    d = date.fromisoformat(day)
    monday = d - timedelta(days=d.weekday())
    return [(monday + timedelta(days=i)).isoformat() for i in range(7)]


def week_range_label(week: str) -> str:
    """ "2026-W41" -> "10-05 ~ 10-11"。"""
    y, m, d = date.fromisocalendar(int(week[:4]), int(week[6:]), 1).timetuple()[:3]
    a = f"{m:02d}-{d:02d}"
    e = date(y, m, d) + timedelta(days=6)
    return f"{a} ~ {e.month:02d}-{e.day:02d}"


def _app_label(name: str, limit: int = 12) -> str:
    """表里和图上都尽量一行放得下，太长就截断加省略号。"""
    name = name or "未知"
    return name if len(name) <= limit else name[:limit - 1] + "…"


def _week_dir(week: str, root_day: str | None = None) -> Path:
    """
   周报目录。

    外部约定：日记按「月 / 周 / 天」三级放在 diary/ 下。每天的正文和图在
    `diary/<年-月>/<年-W周>/<日期>/` 里；周报和周图放在**周**目录
    `diary/<年-月>/<年-W周>/`，也就是各天文件夹的上一级。

    这里不自己拼路径，走 `Store.week_dir()`；旧版 store 没有这个方法时，
    退回「天目录的上一级」。要改目录规则就改 store.py，这里跟着走。

    root_day 决定落在哪个月目录，不传就按该周周一。跨年那一周（周一在 12 月、
    周号属于次年）因此会落到 12 月目录下 —— 和 store.py 的做法一致：
    月目录按日期归属，周号只用于分组。
    """
    day = root_day or _monday_of(week)
    store = Store(day)
    fn = getattr(store, "week_dir", None)
    if callable(fn):
        d = fn(day)
    else:
        # 旧版 store：diary_dir 曾经返回周目录
        legacy = getattr(store, "diary_dir", None)
        d = legacy(day) if callable(legacy) else store.diary_path(day).parent
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------
#  聚合
# ---------------------------------------------------------------
def aggregate_week(week: str) -> dict:
    """
    把一周 7 天的事件各自算一遍再合并。

    没数据的那天仍然占一行（active_seconds=0、has_data=False）——
    缺行会让「这周记了几天」这件事看不出来，而它恰好是周报最该说的一句话。
    """
    days: list[dict] = []
    app_seconds: dict[str, float] = {}
    total = 0.0
    with_data = 0
    best_day, best_sec = "", 0.0

    for d in week_days(_monday_of(week)):
        store = Store(d)
        try:
            stats = compute(store.read_events())
        except Exception:
            # 单天算不出来不该毁掉整周，那天就当没记
            stats = {}

        sec = int(stats.get("total_active_seconds") or 0)
        apps = []
        for a in stats.get("apps", []) or []:
            n = a.get("name") or "未知"
            s = float(a.get("seconds") or 0)
            apps.append({"name": n, "seconds": int(s),
                         "share": round(s / sec, 4) if sec else 0.0})
            app_seconds[n] = app_seconds.get(n, 0.0) + s

        days.append({
            "day": d,
            "weekday": WEEKDAY_CN[date.fromisoformat(d).weekday()],
            "active_seconds": sec,
            "apps": apps,
            "top_app": apps[0]["name"] if apps else "",
            "has_data": sec > 0 and bool(apps),
        })

        total += sec
        if sec > 0:
            with_data += 1
        if sec > best_sec:              # 严格大于 -> 并列时取靠前的那天，结果稳定
            best_day, best_sec = d, sec

    ranked = sorted(app_seconds.items(), key=lambda kv: -kv[1])
    week_apps = [{
        "name": n,
        "seconds": int(s),
        "share": round(s / total, 4) if total else 0.0,
    } for n, s in ranked if s > 0]

    return {
        "week": week,
        "days": days,
        "apps": week_apps,
        "total_active_seconds": int(total),
        "days_with_data": with_data,
        "best_day": best_day,
        "best_day_seconds": int(best_sec),
    }


def _monday_of(week: str) -> str:
    """周标识 -> 那周周一的日期。周报要展示的日期都从这里推。"""
    return date.fromisocalendar(int(week[:4]), int(week[6:]), 1).isoformat()


def _week_plan_summary(week: str, log) -> dict:
    """
    把一周 7 天的计划完成情况加起来。

    沿用 `analysis.plan_review()`：完成与否以**你打的那个勾**为准。
    某天没计划（plan 为空）就不进分母 —— 否则「没设计划」会被算成
    「计划没完成」，完成率会凭空变难看。

    计划从哪来：优先读那天的 `summary-<day>.json` 里的**计划快照**。

    为什么不用 `config.load_config()`：计划只存在于 config.json 这一个全局
    文件里，它记的永远是「当前」的计划。周日生成周报时再去读它，拿到的
    是你最后改过的那一份，不是周一到周日各自的 —— 实测出过「这天没有
    记录、却显示计划 1/1」这种矛盾。

    只有在没有快照时（比如升级前的老数据）才退回读 config。

    注意退回是**按天**发生的：某一周里若只有部分天数有快照，那几天用快照、
    其余天用当前 config，汇总出来的完成率因此可能混着两种口径。这是兼容
    老数据的折中 —— 有旧数据的人至少还能看到一份周报，而不是一片空白。
    新数据每天都会写快照，不会走到这条路。
    """
    done = 0
    tot = 0
    day_rows: list[dict] = []

    for d in week_days(_monday_of(week)):
        store = Store(d)
        try:
            snap = store.read_summary(d) or {}
            plan = snap.get("plan")
            plan_done = snap.get("plan_done")

            if not plan:
                # 没有快照：退回读当前配置（老数据兼容路径）
                cfg = load_config()
                plan = cfg.get("plan") or []
                plan_done = cfg.get("plan_done") or {}
            if not isinstance(plan_done, dict):
                plan_done = {}
            if not plan:
                continue

            stats = compute(store.read_events())
            rev = plan_review(stats, plan, plan_done, store.read_sessions())
        except Exception as e:
            log(f"[周报] {d} 计划汇总失败，跳过: {e}")
            continue
        n = int(rev.get("total") or 0)
        if not n:
            continue
        done += int(rev.get("done") or 0)
        tot += n
        day_rows.append({"day": d, "done": int(rev.get("done") or 0), "total": n})

    return {"done": done, "total": tot,
            "rate": round(done / tot, 3) if tot else 0.0,
            "days": day_rows}


# ---------------------------------------------------------------
#  Markdown
# ---------------------------------------------------------------
def _md_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def build_weekly_markdown(week: str, agg: dict, plan_sum: dict,
                          summary: str, chart_rel: str = "") -> str:
    """拼周报正文。刻意短：7 行概览 + 8 行软件 + 一行计划 + 一段总结。"""
    L: list[str] = []
    L.append(f"# {week} 周报（{week_range_label(week)}）")
    L.append("")

    if chart_rel:
        L.append(f"![每日软件时间分布]({chart_rel})")
        L.append("")

    # ---- 每日概览 ----
    # 没记录的那天也列出来：一眼能看出这周空在哪几天，
    # 比只列有数据的日子诚实（也顺带解释为什么总时长不高）。
    L.append("## 每日概览")
    L.append("")
    L.append(_md_row(["日期", "星期", "活跃", "主要软件", "计划"]))
    L.append(_md_row(["---"] * 5))
    plan_by_day = {r["day"]: r for r in (plan_sum.get("days") or [])}
    for d in agg.get("days", []):
        p = plan_by_day.get(d.get("day"))
        L.append(_md_row([
            d.get("day", "")[5:],
            d.get("weekday", ""),
            fmt_duration(d.get("active_seconds", 0)) if d.get("has_data") else "—",
            _app_label(d.get("top_app", "")) if d.get("has_data") else "（无记录）",
            f"{p['done']}/{p['total']}" if p else "—",
        ]))
    L.append("")
    L.append(_week_line(agg))
    L.append("")

    # ---- 软件分配 ----
    L.append("## 本周软件分配")
    L.append("")
    if agg.get("apps"):
        L.append(_md_row(["软件", "时长", "占比"]))
        L.append(_md_row(["---"] * 3))
        for a in agg["apps"][:MAX_APPS_ROWS]:
            L.append(_md_row([_app_label(a["name"], 16),
                              fmt_duration(a["seconds"]),
                              f"{a['share'] * 100:.1f}%"]))
    else:
        L.append("_本周没有记录到窗口活动。_")
    L.append("")

    # ---- 计划完成 ----
    L.append("## 计划完成")
    L.append("")
    if plan_sum.get("total"):
        L.append(f"累计完成 **{plan_sum['done']}/{plan_sum['total']}**"
                 f"（{plan_sum['rate'] * 100:.0f}%）")
    else:
        L.append("_本周没有制定计划。_")
    L.append("")

    # ---- 总结 ----
    L.append("## 总结")
    L.append("")
    L.append(summary or "_（没生成总结）_")
    L.append("")
    return "\n".join(L)


def _week_line(agg: dict) -> str:
    """一句话说清这周：记了几天、一共多久、最忙哪天。"""
    line = (f"本周记录 {agg.get('days_with_data', 0)} 天，"
            f"活跃合计 **{fmt_duration(agg.get('total_active_seconds', 0))}**")
    if agg.get("best_day"):
        line += (f"，最忙的是 {agg['best_day']}"
                 f"（{fmt_duration(agg.get('best_day_seconds', 0))}）")
    return line + "。"


# ---------------------------------------------------------------
#  周图：每天的软件时间分布
# ---------------------------------------------------------------
def render_weekly_chart(agg: dict, week: str, out_dir: Path) -> Path | None:
    """
    7 行（周一..周日）横向堆叠条：一眼看出每天用了多久、由哪些软件组成。

    为什么按「全周最长的那天」归一化，而不是按各自的比例：这样横条长度
    就等于当天的活跃时长，行与行之间可以直接比长短 —— 读图的人不用换算。
    """
    from PIL import Image, ImageDraw

    days = agg.get("days") or []
    if not days:
        return None

    # 图例的口径由整天合计决定，和上面的汇总表保持一致
    week_apps = agg.get("apps") or []

    max_sec = max([int(d.get("active_seconds") or 0) for d in days] or [0]) or 1

    W = 1180
    pad = 36
    title_h = 96
    label_w = 96                  # 「10-05 一」
    val_w = 130                   # 「6小时40分」
    name_font_size = 17
    val_font_size = 16
    f_title = _font(28)
    f_sub = _font(15)
    f_row = _font(name_font_size)
    f_val = _font(val_font_size)
    f_small = _font(14)

    # 先量图例需要多宽，再定绘图区 —— 免得图例最长那条被切掉
    # （宽度虽然要拿字体量，但字体此时已经建好了）
    _probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    legend = [a["name"] for a in week_apps[:LEGEND_TOP]]
    if len(week_apps) > LEGEND_TOP:
        legend.append(OTHER_LABEL)
    legend_w = 0
    for n in legend:
        legend_w = max(legend_w, int(_probe.textlength(_app_label(n, 14), font=f_row)))
    right_w = max(140, min(280, legend_w + 44))

    bar_x = pad + label_w
    bar_max_w = W - pad - right_w - val_w - bar_x
    if bar_max_w < 120:               # 窗口再窄也得有得看
        bar_max_w = 120

    H = title_h + len(days) * 44 + 84
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # ---- 标题 ----
    d.text((pad, 24), f"{WEEK_TITLE} · {week}", font=f_title, fill=FG)
    d.text((pad, 64), week_range_label(week), font=f_sub, fill=MUTED)
    stat = (f"活跃合计 {fmt_duration(agg.get('total_active_seconds', 0))}"
            f"   记录 {agg.get('days_with_data', 0)}/7 天")
    d.text((W - pad - d.textlength(stat, font=f_sub), 64), stat,
           font=f_sub, fill=MUTED)
    d.line([(pad, title_h - 12), (W - pad, title_h - 12)], fill=GRID, width=1)

    # ---- 图例（放右侧，图上就不必再加一层高度）----
    #
    # 颜色只由「是不是其它」和它在图例里的序号决定，和它在某一天是否出现无关。
    # 曾经按「当天第几段」取色，结果同一个软件在不同日子颜色不同，图就没法看了。
    color_of: dict[str, tuple[int, int, int]] = {}
    lx = W - pad - right_w + 16
    ly = title_h + 14
    for i, n in enumerate(legend):
        # 「其它」用固定的中性灰：它不是一个软件，给彩色会让人以为是个应用。
        # （不然它还会和 PALETTE 里对应序号的那个真软件撞色。）
        color = OTHER_COLOR if n == OTHER_LABEL else PALETTE[i % len(PALETTE)]
        color_of[n] = color
        d.rounded_rectangle([lx, ly + 4, lx + 14, ly + 18], radius=3, fill=color)
        d.text((lx + 22, ly), _app_label(n, 14), font=f_small, fill=FG)
        ly += 26

    # ---- 每天一行 ----
    for i, day in enumerate(days):
        y = title_h + 12 + i * 44
        d.text((pad, y + 4), f"{day.get('day', '')[5:]} {day.get('weekday', '')}",
               font=f_row, fill=FG)

        # 底条画满整行宽度：它同时是「这条刻度有多宽」的参照，
        # 「无记录」那天的空条也靠它
        d.rounded_rectangle([bar_x, y + 4, bar_x + bar_max_w, y + 26],
                            radius=6, fill=GRID)

        sec = int(day.get("active_seconds") or 0)
        if not day.get("has_data") or sec <= 0:
            d.text((bar_x + 10, y + 5), "无记录", font=f_val, fill=MUTED)
        else:
            apps = day.get("apps") or []
            # 这一天的条总长：按全周最长的一天归一化。
            #
            # 这个系数必须乘在这里。踩过的坑：底条和数据条都用了 bar_max_w，
            # 于是每天都画满整行 —— 横条长度完全不反映时长，七天看起来一样长，
            # 而「一眼看出每天用了多久」正是这张图唯一要说的事。
            day_w = max(3, int(round(bar_max_w * sec / max_sec)))
            # 各段按「全天合计」的比例切分。
            #
            # 不能用各段自己乘系数：`a["seconds"]` 是四舍五入过的整数，
            # 各段加起来会和 sec 差几秒到几十秒，差额全落到最后一段上，
            # 实测末段会短掉 2~5 像素（看起来像被削了一刀）。
            wsum = sum(float(a.get("seconds") or 0) for a in apps) or float(sec)
            x = bar_x
            cum = 0.0
            for a in apps:
                cum += float(a.get("seconds") or 0)
                x2 = bar_x + int(round(day_w * cum / wsum))
                # 段间留 1 像素缝：同色相邻时也能看出这是两段。
                # 只在右侧缩，不往左扩 —— 否则段会叠到前一段上。
                seg_end = x2 - 1 if x2 - x >= 4 else x2
                color = color_of.get(a["name"], OTHER_COLOR)
                if seg_end > x:
                    # 用直角矩形堆叠，不用圆角：圆角矩形的四个角会让相邻两段
                    # 之间出现一圈背景色，整条看起来像「一串珠子」而不是一根条。
                    # （单色横条用圆角没问题，那是 charts.py 里每天那张图的做法。）
                    d.rectangle([x, y + 4, seg_end, y + 26], fill=color)
                x = x2

        val = fmt_duration(sec) if sec else "—"
        # 右对齐：时长位数不同也能对齐成一列
        d.text((bar_x + bar_max_w + val_w - 8, y + 5), val,
               font=f_val, fill=MUTED, anchor="ra")

    d.text((pad, H - 40),
           "横条长度 = 当天活跃时长（按全周最长的一天归一化），分段是各软件占比，"
           "右对齐数字为当天合计", font=f_small, fill=MUTED)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"weekly-chart-{week}.png"
    try:
        img.save(path, "PNG", optimize=True)
    except Exception:
        return None
    return path


# ---------------------------------------------------------------
#  周总结
# ---------------------------------------------------------------
def make_weekly_summary(week: str, agg: dict, plan_sum: dict,
                        tone_name: str, tone_style: str) -> str:
    """
    让文本模型写一段周总结。

    只喂它聚合后的数字，不喂截图描述 —— 周报不要「屏幕印象」，
    喂了它就会开始复述，长度立刻失控。
    """
    total = agg.get("total_active_seconds", 0) or 0
    apps_line = "、".join(
        f"{a['name']} {fmt_duration(a['seconds'])}" for a in (agg.get("apps") or [])[:3])

    if plan_sum.get("total"):
        plan_line = (f"{plan_sum['done']}/{plan_sum['total']}"
                     f"（完成率 {plan_sum['rate'] * 100:.0f}%）")
    else:
        plan_line = "本周没有制定计划"

    prompt = f"""周次：{week}（{week_range_label(week)}）

本周软件使用情况：
  活跃总时长：{fmt_duration(total)}
  有记录的天数：{agg.get('days_with_data', 0)} 天（一周 7 天）
  最忙的一天：{agg.get('best_day') or '无'}（{fmt_duration(agg.get('best_day_seconds', 0))}）
  主要软件：{apps_line or '（没有记录）'}

本周计划完成情况：
  {plan_line}

请写一段这一周的总结，要求：
1. 用{tone_name}的语气：{tone_style}。
2. 长度 3 到 5 句话，说人话，别用「首先其次最后」这种结构。
3. 必须提到：活跃总时长、有几天有记录、最忙是哪一天、计划完成得怎么样。
4. 必须提到本周主要用的那几个软件。
5. 不要用 markdown 标题、不要列表、不要表情符号、不要问句。
6. 直接开始写，不要写「总结：」这样的前缀。"""

    sys_p = ("你是一个很了解用户的中文助手，负责写每周总结。"
             "说话自然、口语化，有判断力，只看数字给结论，不编造具体事件。")
    out = diary.ask_text(prompt, sys_p, num_predict=320, temperature=0.85)
    return (out or "").strip()


# ---------------------------------------------------------------
#  主流程
# ---------------------------------------------------------------
def run_weekly(week: str, progress=None, logger=None) -> dict:
    """
    完整流程：聚合 -> 计划汇总 -> 语气分档 -> 写总结 -> 画图 -> 写周报。

    返回值里的路径都是字符串，方便直接塞进界面。
    """
    log = logger or (lambda *a, **k: None)

    def rep(stage, done=0, total=0, msg=""):
        if progress:
            progress(stage, done, total, msg)

    log(f"[周报] 开始生成 {week}")
    rep("聚合", 0, 0, "正在汇总这一周")
    agg = aggregate_week(week)
    log(f"[周报] 活跃合计 {fmt_duration(agg['total_active_seconds'])}，"
        f"记录 {agg['days_with_data']}/7 天")

    rep("计划", 0, 0, "正在对计划")
    plan_sum = _week_plan_summary(week, log)

    # 语气档位由完成率算出来，模型只负责用这个语气写（理由见 diary.py）
    tone_name, tone_style = diary.pick_tone(plan_sum["rate"])
    log(f"[周报] 完成率 {plan_sum['rate'] * 100:.0f}% -> 语气「{tone_name}」")

    rep("写总结", 0, 0, "正在根据这周的完成情况写总结")
    summary = make_weekly_summary(week, agg, plan_sum, tone_name, tone_style)

    # 该周任意一天都指向同一个月份目录，拿周一来取最直观
    store = Store(_monday_of(week))
    target_dir = _week_dir(week, store.day)

    chart_rel = ""
    chart_path = None
    rep("画图", 0, 0, "生成周图")
    try:
        chart_path = render_weekly_chart(agg, week, target_dir)
        if chart_path:
            chart_rel = chart_path.name
    except Exception as e:
        log(f"[周报] 画图失败: {e}")

    md = build_weekly_markdown(week, agg, plan_sum, summary, chart_rel)

    # 文件名和目录都交给 store.py：它约定周报叫 weekly-<周号>.md
    # （list_weeklies 靠这个前缀把周报和日报分开）。旧版 store 没这套方法时，
    # 退回「周目录 / 周号.md」。
    write_fn = getattr(store, "write_weekly", None)
    try:
        if callable(write_fn):
            out = write_fn(md, week, store.day)
        else:
            path_fn = getattr(store, "weekly_path", None)
            out = (path_fn(week, store.day) if callable(path_fn)
                   else target_dir / f"weekly-{week}.md")
            out.write_text(md, encoding="utf-8")
    except Exception as e:
        log(f"[周报] 写文件失败，退回周目录: {e}")
        out = target_dir / f"weekly-{week}.md"
        out.write_text(md, encoding="utf-8")
    log(f"[周报] 已写入 {out}")

    rep("完成", 1, 1, f"周报：{out.name}")
    return {
        "week": week,
        "chart": str(chart_path) if chart_rel else None,
        "diary": str(out),
        "summary": summary,
        "tone": tone_name,
        "agg": agg,
        "plan": plan_sum,
    }
