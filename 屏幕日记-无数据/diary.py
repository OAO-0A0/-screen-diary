"""
屏幕日记 —— 日记生成

流程：跑视觉分析 → 算统计 → 对计划 → 按情绪分档写总结 → 画图 → 拼 Markdown

情绪分档为什么放在代码里而不是交给模型：
  你要求「总结的情绪要根据完成情况不同」。如果让模型自己判断完成得好不好，
  它经常过分宽容（什么都说「你今天已经很棒了」）。所以档位由完成率算出来，
  模型只负责「用这个语气写」。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

from analysis import compute, fmt_duration, plan_review
from config import (DIARY_DIR, OLLAMA_API, TEXT_MODEL, VISION_MODEL,
                    pretty_name)
from screenshots import day_shot_usage
from store import Store
import vision


# ---------------------------------------------------------------
#  情绪分档
# ---------------------------------------------------------------
TONES = [
    (1.20, "得瑟", "今天超额完成了，语气要得意但不讨厌，像完成 KPI 后忍不住炫耀"),
    (0.80, "满意", "基本按计划完成了，语气踏实、简短认可，别过度吹捧"),
    (0.50, "阴阳怪气", "完成得不太行，用阴阳怪气的语气，带点调侃和反问，别骂人"),
    (0.20, "阴阳怪气加强", "完成得很差，语气要更欠一点，像看不下去的朋友在损你，但还是善意"),
    (0.00, "破防自嘲", "基本什么都没完成，语气要自嘲、破防，像计划被自己当笑话看了"),
]


def pick_tone(rate: float) -> tuple[str, str]:
    for threshold, name, style in TONES:
        if rate >= threshold:
            return name, style
    return TONES[-1][1], TONES[-1][2]


# ---------------------------------------------------------------
#  纯文本模型调用
# ---------------------------------------------------------------
def _post(path: str, payload: dict, timeout: float):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_API}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    return urllib.request.urlopen(req, timeout=timeout)


def ask_text(prompt: str, system: str = "", model: str = TEXT_MODEL,
             timeout: float = 300, num_predict: int = 400,
             temperature: float = 0.8) -> str:
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    payload = {
        "model": model, "messages": msgs, "stream": False, "think": False,
        "keep_alive": "5m",
        "options": {"num_ctx": 8192, "num_predict": num_predict,
                    "temperature": temperature, "top_p": 0.95},
    }
    try:
        with _post("/api/chat", payload, timeout) as r:
            obj = json.loads(r.read().decode("utf-8"))
        txt = (obj.get("message", {}).get("content") or "").strip()
        import re
        txt = re.sub(r"<think\b[^>]*>.*?</think\s*>", "", txt, flags=re.S | re.I)
        return txt.strip()
    except urllib.error.HTTPError as e:
        return f"（文本模型调用失败：HTTP {e.code}）"
    except Exception as e:
        return f"（文本模型调用失败：{e}）"


# ---------------------------------------------------------------
#  视觉批量分析
# ---------------------------------------------------------------
def analyze_shots(day: str | None = None, progress=None,
                  should_stop=None) -> dict:
    """
    把当天的截图逐张描述。返回 {截图文件名: 描述}。
    进度回调 progress(done, total, 描述)。
    """
    store = Store(day)
    shot_dir = store.shot_dir()
    shots = sorted(shot_dir.glob("*.jpg"))
    total = len(shots)
    results: dict[str, str] = {}

    if total == 0:
        return results

    if not vision.has_vision(VISION_MODEL):
        for p in shots:
            results[p.name] = "（视觉模型不可用，跳过）"
        return results

    for i, p in enumerate(shots, 1):
        if should_stop and should_stop():
            break
        try:
            text, el = vision.describe_image(p)
            results[p.name] = text or "（没读出内容）"
        except Exception as e:
            results[p.name] = f"（分析失败：{e}）"
            el = 0
        if progress:
            progress(i, total, results[p.name])
    return results


# ---------------------------------------------------------------
#  总结
# ---------------------------------------------------------------
def make_summary(stats: dict, review: dict, tone_name: str, tone_style: str,
                 descriptions: list[str], day: str) -> str:
    apps_line = "、".join(
        f"{a['name']} {fmt_duration(a['seconds'])}" for a in stats["apps"][:8])

    plan_lines = []
    for r in review["rows"]:
        mark = "已完成" if r["done"] is True else (
            "明确没完成" if r["done"] is False else "没打勾，未知")
        plan_lines.append(
            f"  {r['start']}-{r['end']} {r['task']}：计划 {r['planned_minutes']} 分钟，"
            f"相关软件实际 {r['actual_minutes']} 分钟，{mark}")

    sample = descriptions[:40]
    seen = []
    for d in sample:
        if d and d not in seen:
            seen.append(d)
    seen = seen[:25]

    prompt = f"""日期：{day}

今天的软件使用情况：
  {apps_line or '（没有记录）'}
  活跃总时长：{fmt_duration(stats['total_active_seconds'])}
  中间离开电脑：{fmt_duration(stats['away_seconds'])}
  窗口切换次数：{stats['window_events']}，截图 {stats['shots']} 张

今天定下的计划与完成情况：
{chr(10).join(plan_lines) if plan_lines else '  （今天没设计划）'}
  完成率：{review['done']}/{review['total']}

从截图里读到的片段（模型对屏幕的印象，可能不准）：
{chr(10).join('  - ' + s for s in seen) if seen else '  （没有截图）'}

请写一段今天的总结，要求：
1. 用{tonename(tone_name)}的语气：{tone_style}。
2. 长度 3 到 5 句话，说人话，别用「首先其次最后」这种结构。
3. 要具体提到今天实际干了什么、计划完成得怎么样，不要空泛。
4. 不要用 markdown 标题、不要列表、不要表情符号。
5. 直接开始写，不要写「总结：」这样的前缀。"""

    sys_p = ("你是一个很了解用户的中文助手，负责写每日总结。"
             "说话自然、口语化，有判断力，不回避指出问题，但保持善意。")
    out = ask_text(prompt, sys_p, num_predict=350, temperature=0.85)
    return out.strip()


def tonename(t: str) -> str:
    return t


# ---------------------------------------------------------------
#  拼 Markdown
# ---------------------------------------------------------------
BAR_WIDTH = 24   # 保留：图表里画字符条可能还要用，暂不删除

# 日报里软件分配最多列几项（其余并成「其它 N 项」）。
# 日报要能一眼看完，列太多反而没人看。
DAILY_TOP_APPS = 5


def build_markdown(day: str, stats: dict, review: dict, summary: str,
                   descriptions: dict, chart_rel: str = "") -> str:
    """
    拼装日记正文。

    注意：这里**不写**「今日情绪档位」这类字样。语气档位是内部实现细节，
    只用来决定总结的写法，不该出现在成品页面上 —— 页面上你读到的应该
    就是那段总结本身，而不是它被分到哪一档。
    """
    try:
        wd = "一二三四五六日"[date.fromisoformat(day).weekday()]
    except Exception:
        wd = ""

    L: list[str] = []
    L.append(f"# {day} 星期{wd}")
    L.append("")

    # ---- 软件分配 ----
    #
    # 只列前 5 项。日报的定位是「快速回顾今天」，不是完整审计 ——
    # 列 15 项会让人不想看，而且第 6 名往后基本都是零头。
    # 需要全量的时候看图表（图里也是按占比排序的）。
    L.append("## 软件分配")
    L.append("")
    if stats["apps"]:
        L.append("| 软件 | 时长 | 占比 |")
        L.append("|---|---|---|")
        for a in stats["apps"][:DAILY_TOP_APPS]:
            L.append(f"| {a['name']} | {fmt_duration(a['seconds'])} | {a['share']*100:.1f}% |")
        rest = stats["apps"][DAILY_TOP_APPS:]
        if rest:
            rest_sec = sum(a["seconds"] for a in rest)
            rest_share = sum(a["share"] for a in rest)
            L.append(f"| 其它 {len(rest)} 项 | {fmt_duration(rest_sec)} | {rest_share*100:.1f}% |")
        L.append("")
        L.append(f"- 活跃总时长：**{fmt_duration(stats['total_active_seconds'])}**")
        L.append(f"- 离开电脑：{fmt_duration(stats['away_seconds'])}")
        if stats.get("first_event"):
            L.append(f"- 记录区间：{stats['first_event'][-8:]} – {stats.get('last_event','')[-8:]}")
    else:
        L.append("_今天没有记录到窗口活动。_")
    L.append("")

    if chart_rel:
        L.append(f"![软件使用分配]({chart_rel})")
        L.append("")

    # ---- 计划 vs 实际 ----
    L.append("## 计划 vs 实际")
    L.append("")
    if review["rows"]:
        L.append("| 计划时段 | 任务 | 计划 | 相关软件实际 | 完成 |")
        L.append("|---|---|---|---|---|")
        for r in review["rows"]:
            mark = "对" if r["done"] is True else ("错" if r["done"] is False else "—")
            L.append(f"| {r['start']}-{r['end']} | {r['task']} | "
                     f"{r['planned_minutes']}分 | {r['actual_minutes']}分 | {mark} |")
        L.append("")
        L.append(f"完成率：**{review['done']}/{review['total']}**"
                 f"（{review['rate']*100:.0f}%）")
    else:
        L.append("_今天没有设计划。_")
    L.append("")

    if review.get("manual_sessions"):
        L.append("### 手动记录的段")
        L.append("")
        L.append("| 时段 | 任务 | 时长 |")
        L.append("|---|---|---|")
        for s in review["manual_sessions"]:
            L.append(f"| {s['start']}–{s['end']} | {s['task']} | {s['minutes']}分 |")
        L.append("")

    # ---- 总结 ----
    L.append("## 总结")
    L.append("")
    L.append(summary or "_（没生成总结）_")
    L.append("")

    # ---- 屏幕印象（折叠） ----
    #
    # 每张截图的一句话描述，默认收起 —— 想看屏幕内容的时候点开。
    #
    # 为什么折叠而不是删掉：这段是「那天你到底在屏幕上看什么」的唯一记录，
    # 而截图分析完就删了，过后再也补不出来。它是几十行、视觉模型也常认错
    # （实测把微信认成「客户关系管理软件」），所以不该默认铺开，但更不该丢。
    #
    # 周报里刻意**没有**这一段：一周几十张图的描述堆起来没人看。
    if descriptions:
        L.append("<details>")
        L.append(f"<summary>屏幕印象（{len(descriptions)} 张截图，点开看）"
                 f"</summary>")
        L.append("")
        for name, desc in sorted(descriptions.items()):
            # 文件名是 HHMM.jpg 形式（截图时的时刻），转成 09:32 更好读
            stem = Path(name).stem
            label = f"{stem[:2]}:{stem[2:4]}" if len(stem) >= 4 else stem
            L.append(f"- `{label}` {desc}")
        L.append("")
        L.append("</details>")
        L.append("")

    L.append("---")
    L.append(f"_由本地模型生成于 {datetime.now():%Y-%m-%d %H:%M}_")
    return "\n".join(L)


# ---------------------------------------------------------------
#  主流程
# ---------------------------------------------------------------
def run_full_analysis(day: str | None = None, progress=None,
                      should_stop=None, logger=None,
                      delete_shots_after: bool = True,
                      keep_model_loaded: bool = False) -> dict:
    """
    完整流程：视觉分析 -> 统计 -> 计划对比 -> 总结 -> 日记。
    progress 回调 (阶段名, 已完成, 总数, 消息)。

    delete_shots_after  跑完删掉当天截图（默认开，图是临时的）
    keep_model_loaded   跑完**不**卸载模型。批量补分析时用：整批跑完
                        由调用方卸一次，省掉每天一次的加载等待。
    """
    log = logger or (lambda *a, **k: None)
    store = Store(day)
    day = store.day

    def rep(stage, done=0, total=0, msg=""):
        if progress:
            progress(stage, done, total, msg)

    # 1) 视觉分析
    shot_dir = store.shot_dir()
    n_shots, mb = day_shot_usage(shot_dir)
    rep("视觉分析", 0, n_shots, f"共 {n_shots} 张截图（{mb:.0f} MB）")
    log(f"[分析] 开始，{n_shots} 张截图")

    def vp(i, total, text):
        rep("视觉分析", i, total, text)

    descriptions = analyze_shots(day, progress=vp, should_stop=should_stop)

    # 2) 统计
    rep("统计", 0, 0, "正在算时长")
    events = store.read_events()
    sessions = store.read_sessions()
    stats = compute(events)
    log(f"[分析] 活跃 {fmt_duration(stats['total_active_seconds'])}, "
        f"{len(stats['apps'])} 个软件")

    # 3) 计划对比
    import config as cfg
    c = cfg.load_config()
    cfg_plan = c.get("plan", [])
    cfg_plan_done = c.get("plan_done", {})
    review = plan_review(stats, cfg_plan, cfg_plan_done, sessions)

    # 4) 总结。档位只用来决定写法，不显示在界面上。
    tone_name, tone_style = pick_tone(review["rate"])
    rep("写总结", 0, 0, "正在根据今天的完成情况写总结")
    log(f"[分析] 完成率 {review['rate']*100:.0f}% -> 语气「{tone_name}」")

    desc_list = [v for k, v in sorted(descriptions.items())]
    summary = make_summary(stats, review, tone_name, tone_style, desc_list, day)
    if should_stop and should_stop():
        summary = summary or "（分析被中止）"

    # 5) 画图（放在日记同目录，用相对路径引用，方便你整体搬走）
    chart_rel = ""
    try:
        rep("画图", 0, 0, "生成分配图")
        import charts
        target_dir = store.diary_path(day).parent
        chart_path = charts.render_app_chart(stats, day, review, target_dir)
        if chart_path:
            chart_rel = chart_path.name
        timeline_path = charts.render_timeline(
            stats, day, target_dir) if stats.get("timeline") else None
    except Exception as e:
        log(f"[画图] 失败: {e}")

    # 6) 写日记
    md = build_markdown(day, stats, review, summary, descriptions, chart_rel)
    path = store.write_diary(md, day)
    log(f"[分析] 日记已写入 {path}")

    result = {
        "day": day, "diary": str(path), "stats": stats, "review": review,
        "summary": summary, "tone": tone_name, "shots": n_shots,
        "descriptions": descriptions,
    }
    # 计划快照怎么取 —— 这里有个容易搞错的地方，说明白：
    #
    #   计划只存在于 config.json 这一个全局文件里，它记的永远是「当前」
    #   的计划。周日生成周报时再去读它，拿到的是你最后改过的那一份，
    #   而不是周一到周日各自的计划 —— 实测出过「这天没有记录，却显示
    #   计划 1/1」这种矛盾。
    #
    #   所以计划要**落快照**。但快照不能被后一次分析冲掉：
    #   如果那天已经有快照（比如补分析某天、或者跑完周报又重算），
    #   就以已有的为准 —— 那才是「那天当时的计划」。
    #   只有在完全没有快照时，才用当前 config 的值补一份。
    _prev = store.read_summary(day) or {}
    plan_snapshot = _prev.get("plan") or cfg_plan
    plan_done_snapshot = _prev.get("plan_done")
    if not isinstance(plan_done_snapshot, dict):
        plan_done_snapshot = cfg_plan_done

    store.write_summary({
        "day": day, "generated": datetime.now().isoformat(timespec="seconds"),
        "tone": tone_name,
        "apps": stats["apps"],
        "review": review,
        "summary": summary,
        "shots": n_shots,
        "plan": plan_snapshot,
        "plan_done": plan_done_snapshot,
        # 逐张截图的描述也存一份：截图分析完就删了，这是屏幕内容的唯一记录，
        # 过后想补出来只能重新跑一遍视觉分析（而图已经没了）。
        "descriptions": descriptions,
    }, day)

    # 7) 删图
    if delete_shots_after:
        removed = 0
        for f in shot_dir.glob("*.jpg"):
            try:
                f.unlink()
                removed += 1
            except Exception:
                pass
        log(f"[分析] 已删除 {removed} 张截图，释放 {mb:.0f} MB")

    # 7.5) 把模型踢出显存
    #
    # vision.unload() 一直写好了却没人调用，于是分析完显存会再占着
    # 5–10 分钟才由 Ollama 超时释放（视觉模型 keep_alive 是 10m）。
    # 这跟「不要占过多资源」是相悖的：你分析完想打游戏，显存还被占着。
    # 这里显式释放一次，失败也不影响结果（unload 内部吞掉所有异常）。
    #
    # 批量补分析时除外：keep_model_loaded=True 让调用方在整批跑完后
    # 自己卸一次。否则每天都「卸了再加载」，白等好几次加载时间。
    if not keep_model_loaded:
        try:
            import vision as _v
            _v.unload(VISION_MODEL)
            if TEXT_MODEL != VISION_MODEL:
                _v.unload(TEXT_MODEL)
            log("[分析] 已释放显存")
        except Exception:
            pass

    # 8) 周日的话，顺手把这一周的周报也生成了
    #
    # 为什么绑在「周日那天分析完」而不是单独一个定时任务：
    # 周报要读这一周 7 天的数据，而数据是随用随生成的。周日这天你分析完，
    # 这一周才刚好齐 —— 此时顺手做掉最自然，也不会漏。
    if _is_week_end(day):
        try:
            import weekly
            rep("周报", 0, 0, "今天是周日，顺带生成本周周报")
            # 写入的日志由 weekly.run_weekly 自己记，这里不重复记一遍
            result["weekly"] = weekly.run_weekly(
                weekly.week_key(day), progress=progress, logger=log)
        except Exception as e:
            log(f"[周报] 生成失败：{e}")

    rep("完成", 1, 1, f"日记：{path.name}")
    return result


def _is_week_end(day: str) -> bool:
    """这天是不是这一周的最后一天（周日）。"""
    try:
        return date.fromisoformat(day).weekday() == 6      # 周一=0 ... 周日=6
    except Exception:
        return False
