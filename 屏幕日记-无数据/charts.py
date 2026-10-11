"""
屏幕日记 —— 可视化出图

产出两张图（放在日记同目录）：
  * chart.png      软件分配横向条形图 + 概览
  * timeline.png   一天的时间线（谁在什么时候占了多久）

用 PIL 画，不用 matplotlib —— 少一个依赖，而且中文控制更直接。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from analysis import fmt_duration

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]

# 配色：够用就行，按序号循环
PALETTE = [
    (43, 108, 176), (192, 86, 33), (56, 161, 105), (213, 63, 140),
    (113, 128, 150), (159, 122, 234), (214, 158, 46), (49, 151, 149),
    (184, 50, 50), (128, 90, 213),
]

BG = (250, 250, 252)
FG = (26, 32, 44)
MUTED = (140, 148, 160)
GRID = (228, 232, 240)


def _font(size: int):
    for c in FONT_CANDIDATES:
        try:
            if Path(c).exists():
                return ImageFont.truetype(c, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _has_glyphs(font, chars: str) -> dict[str, bool]:
    """
    一次性检查字体里有哪些字形。

    微软雅黑不含 ✓/✗ 这类符号，直接画会渲染成空方框（实测确认）。
    判据：拿私用区字符（正常字体一定没有）当对照，
    比较渲染宽度 —— 宽度相同说明都是「缺字形」的方框。
    """
    out = {}
    try:
        ref = font.getbbox("\uE000")
        ref_w = ref[2] if ref else 0
        for ch in chars:
            bb = font.getbbox(ch)
            out[ch] = bool(bb and bb[2] > ref_w)
    except Exception:
        out = {ch: True for ch in chars}
    return out


# 完成标记：优先用勾叉，字体不支持就退回中文
_MARK_CACHE: dict[int, tuple[str, str, str]] = {}


def _marks(font):
    key = id(font)
    if key not in _MARK_CACHE:
        has = _has_glyphs(font, "✓✗·")
        if has.get("✓") and has.get("✗"):
            _MARK_CACHE[key] = ("✓", "✗", "·")
        else:
            # 中文里「对」「错」一定有的，做退路
            _MARK_CACHE[key] = ("对", "错", "·")
    return _MARK_CACHE[key]


def render_app_chart(stats: dict, day: str, review: dict,
                     out_dir: Path | None = None) -> Path | None:
    """软件分配图 + 顶部概览 + 底部计划完成情况。"""
    apps = stats.get("apps", [])[:10]
    if not apps:
        return None

    W = 1250
    pad = 36
    title_h = 96
    row_h = 40
    chart_h = len(apps) * row_h + 20
    plan_h = 0
    rows = review.get("rows", [])
    if rows:
        plan_h = 60 + len(rows) * 30

    H = title_h + chart_h + 40 + plan_h + 70
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    f_title = _font(30)
    f_sub = _font(16)
    f_name = _font(18)
    f_val = _font(17)
    f_small = _font(15)

    # ---- 标题 ----
    try:
        wd = "一二三四五六日"[date.fromisoformat(day).weekday()]
        day_label = f"{day}  星期{wd}"
    except Exception:
        day_label = day
    d.text((pad, 26), "屏幕日记 · 软件使用分配", font=f_title, fill=FG)
    d.text((pad, 66), day_label, font=f_sub, fill=MUTED)

    total = stats.get("total_active_seconds", 0)
    active_txt = f"活跃 {fmt_duration(total)}"
    if stats.get("away_seconds"):
        active_txt += f"   离开 {fmt_duration(stats['away_seconds'])}"
    active_txt += f"   切换 {stats.get('window_events',0)} 次"
    tw = d.textlength(active_txt, font=f_sub)
    d.text((W - pad - tw, 66), active_txt, font=f_sub, fill=MUTED)

    d.line([(pad, title_h - 12), (W - pad, title_h - 12)], fill=GRID, width=1)

    # ---- 条形图 ----
    label_w = 190
    val_w = 150          # 够放「1小时21分  49%」不被切
    bar_x = pad + label_w
    bar_w_max = W - pad - val_w - bar_x
    y = title_h + 14

    for i, a in enumerate(apps):
        color = PALETTE[i % len(PALETTE)]
        name = a["name"]
        if len(name) > 13:
            name = name[:12] + "…"

        # 名字
        d.text((pad, y + 6), name, font=f_name, fill=FG)

        # 底条
        d.rounded_rectangle([bar_x, y + 5, bar_x + bar_w_max, y + 27],
                            radius=6, fill=GRID)

        # 数据条
        w = max(3, int(bar_w_max * float(a.get("share") or 0)))
        d.rounded_rectangle([bar_x, y + 5, bar_x + w, y + 27],
                            radius=6, fill=color)

        # 数值
        val = f"{fmt_duration(a['seconds'])}  {a['share']*100:.0f}%"
        d.text((bar_x + bar_w_max + 12, y + 7), val, font=f_val, fill=MUTED)

        y += row_h

    y += 22

    # ---- 计划 vs 实际 ----
    if rows:
        d.line([(pad, y - 10), (W - pad, y - 10)], fill=GRID, width=1)
        d.text((pad, y + 4), "计划 vs 实际", font=f_name, fill=FG)

        done = review.get("done", 0)
        tot = review.get("total", 0) or 1
        rate = review.get("rate", 0)
        rate_txt = f"完成 {done}/{tot}  ({rate*100:.0f}%)"
        tw = d.textlength(rate_txt, font=f_name)
        d.text((W - pad - tw, y + 4), rate_txt, font=f_name,
               fill=(56, 161, 105) if rate >= 0.8 else
                    ((214, 158, 46) if rate >= 0.5 else (184, 50, 50)))
        y += 36

        for r in rows:
            ok_mark, no_mark, none_mark = _marks(f_name)
            mark = (ok_mark if r["done"] is True else
                    (no_mark if r["done"] is False else none_mark))
            mcolor = ((56, 161, 105) if r["done"] is True else
                      ((184, 50, 50) if r["done"] is False else MUTED))
            d.text((pad + 4, y), mark, font=f_name, fill=mcolor)

            line = (f"{r['start']}-{r['end']}  {r['task']}     "
                    f"计划 {r['planned_minutes']}分 / 实际 {r['actual_minutes']}分")
            d.text((pad + 28, y + 2), line, font=f_small, fill=FG)

            # 计划时长条与相关软件实际时长条对照
            bx = W - pad - 220
            pw = 200
            d.rounded_rectangle([bx, y + 4, bx + pw, y + 20], radius=4, fill=GRID)
            act = min(1.0, (r["actual_minutes"] / r["planned_minutes"])
                      if r["planned_minutes"] else 0)
            if act > 0:
                d.rounded_rectangle([bx, y + 4, bx + int(pw * act), y + 20],
                                    radius=4, fill=mcolor)
            y += 30

    # ---- 页脚 ----
    foot = "本地模型生成 · 截图分析后已删除"
    d.text((pad, H - 40), foot, font=f_small, fill=MUTED)

    out_dir = out_dir or Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    # 文件名不带日期：日期已经由 diary/<年-月>/<年-W周>/<日期>/ 这两级目录
    # 表达了，文件名再带一遍是重复（而且改名/复制时容易和内容不一致）。
    path = out_dir / "chart.png"
    img.save(path, "PNG", optimize=True)
    return path


def render_timeline(stats: dict, day: str, out_dir: Path | None = None) -> Path | None:
    """一天的时间线：横轴是时间，每一行是一个软件，有活动的时段涂色。"""
    apps = stats.get("apps", [])
    tl = stats.get("timeline", [])
    if not tl:
        return None

    names = [a["name"] for a in apps[:12]]
    if not names:
        return None

    W = 1180
    pad = 36
    left = 190
    right = 40
    row_h = 30
    top = 110
    H = top + len(names) * row_h + 60

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f_title = _font(26)
    f_name = _font(16)
    f_small = _font(14)

    d.text((pad, 24), "屏幕日记 · 今日时间线", font=f_title, fill=FG)
    d.text((pad, 62), day, font=f_small, fill=MUTED)

    # 起止时间
    ts = [w.get("t", "") for w in tl if w.get("t")]
    if not ts:
        return None
    t0 = _to_min(ts[0])
    t1 = _to_min(ts[-1])
    # 最后一段要算上它自己的停留时长，否则最后那条会被压成一条线
    last_sec = 0
    for w in reversed(tl):
        if w.get("t"):
            last_sec = w.get("seconds", 0) or 0
            break
    t1 = max(t1 + last_sec / 60, t0 + 1)
    span = t1 - t0

    plot_w = W - left - right
    plot_x = left

    def x_of(minutes: float) -> int:
        """时间 -> 横坐标，并强制夹在绘图区内（防越界画出全宽长条）。"""
        raw = plot_x + plot_w * ((minutes - t0) / span)
        return int(max(plot_x, min(plot_x + plot_w, raw)))

    # 小时刻度
    start_hour = int(t0 // 60)
    end_hour = int(t1 // 60) + 1
    for h in range(start_hour, end_hour + 1):
        x = x_of(h * 60)
        if plot_x <= x <= plot_x + plot_w:
            d.line([(x, top - 8), (x, top + len(names) * row_h + 4)],
                   fill=GRID, width=1)
            d.text((x + 3, top - 26), f"{h:02d}:00", font=f_small, fill=MUTED)

    # 行
    for i, name in enumerate(names):
        y = top + i * row_h
        d.text((pad, y + 5), name[:14], font=f_name, fill=FG)
        color = PALETTE[i % len(PALETTE)]
        for w in tl:
            if w.get("app") != name or not w.get("t"):
                continue
            a = _to_min(w["t"])
            b = a + max(0.5, (w.get("seconds", 0) or 0) / 60)
            x1 = x_of(a)
            x2 = max(x1 + 2, x_of(b))
            d.rounded_rectangle([x1, y + 6, min(x2, plot_x + plot_w), y + 22],
                                radius=3, fill=color)

    d.text((pad, H - 34), "每格是一次窗口切换，宽度是该窗口停留时长",
           font=f_small, fill=MUTED)

    out_dir = out_dir or Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "timeline.png"      # 同 render_app_chart：日期由目录表达
    img.save(path, "PNG", optimize=True)
    return path


def _to_min(hhmmss: str) -> float:
    try:
        h, m, s = (int(x) for x in hhmmss[-8:].split(":"))
        return h * 60 + m + s / 60
    except Exception:
        return 0.0
