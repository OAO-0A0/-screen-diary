"""
屏幕日记 —— 数据存储

设计取舍：事件流用「按天一个 JSONL 文件」，不用数据库。

理由：
  * 追加写，程序崩了也不丢已写的部分（SQLite 事务反而更娇气）
  * 出问题你直接用记事本就能看、能改
  * 一年也就几十万行，几 MB，检索完全不是瓶颈
  * 不用管数据库文件损坏、锁定这些破事

手工记录的「段」存成单个 JSON，因为一天不会有几千段。
日记成品是 Markdown，按月份归目录。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, date, timedelta

# 每月几号之后归档上个月。定在 7 号是给月初补分析留缓冲（截图保留 7 天）。
ARCHIVE_AFTER_DAY = 7
from pathlib import Path

from config import DATA_DIR, DIARY_DIR, SHOT_DIR, ensure_dirs


def today_str(d: date | None = None) -> str:
    return (d or date.today()).strftime("%Y-%m-%d")


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Store:
    """一天的记录：事件流 + 手工段 + 分析结果。"""

    def __init__(self, day: str | None = None):
        ensure_dirs()
        self.day = day or today_str()
        # 写入路径一律是新结构；读取时再用 _path_read 兼容旧结构。
        # 这里保持「写入路径」的语义，因为 append_event 等写入方法用它。
        self.events_path = self._path(self.day, "events", ".jsonl")
        self.sessions_path = self._path(self.day, "sessions", ".json")
        self.summary_path = self._path(self.day, "summary", ".json")
        self._lock = threading.Lock()

    # ---------------- 读取路径（自动兼容旧结构）----------------
    def events_file(self) -> Path:
        """实际该读的事件流文件（新结构优先，退回旧结构）。"""
        return self._path_read(self.day, "events", ".jsonl")

    def sessions_file(self) -> Path:
        return self._path_read(self.day, "sessions", ".json")

    def summary_file(self) -> Path:
        return self._path_read(self.day, "summary", ".json")

    # ---------------- 文件路径 ----------------
    #
    # 按「年月」分目录存：
    #
    #   data/2026-09/events-2026-09-28.jsonl
    #   data/2026-09/sessions-2026-09-28.json
    #   data/2026-09/summary-2026-09-28.json
    #   data/2026-09/archive-2026-09.zip        <- 归档包
    #
    # 为什么分目录：一天三个文件，一年就是 1100 个，全堆在 data/ 里
    # 看着很难受（体积其实很小，一年才 1.4 MB）。
    #
    # 兼容旧数据：早期版本直接放在 data/ 根下。读取时会退回旧位置，
    # 所以升级后不用做任何处理也能看到老记录（写入永远走新结构）。
    @staticmethod
    def day_dir(day: str) -> Path:
        """某天数据所在的月目录（形如 data/2026-09）。"""
        y, m, _ = day.split("-")[:3]
        d = DATA_DIR / f"{y}-{m}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @classmethod
    def _path(cls, day: str, kind: str, ext: str) -> Path:
        """新结构的路径。写入一律用它。"""
        return cls.day_dir(day) / f"{kind}-{day}{ext}"

    @classmethod
    def _path_read(cls, day: str, kind: str, ext: str) -> Path:
        """
        读取用的路径：新结构优先，没有就退回旧结构（data/ 根下）。

        为什么要退回：升级前存下的记录不该因为改了目录就消失。

        注意这里**不能**调 `_path()` —— 它会顺手 mkdir 月目录，
        然后返回那个新路径。新路径当然不存在（数据还躺在根目录），
        于是读出来是空的 —— 实测就是这么把旧数据「读没了」的。
        所以先纯拼路径探一下，最后才创建目录。
        """
        y, m, _ = day.split("-")[:3]
        new = DATA_DIR / f"{y}-{m}" / f"{kind}-{day}{ext}"
        if new.exists():
            return new
        legacy = DATA_DIR / f"{kind}-{day}{ext}"
        if legacy.exists():
            return legacy
        # 两边都没有：返回新路径，调用方写的时候会建目录
        cls.day_dir(day)
        return new

    # ---------------- 事件流 ----------------
    def append_event(self, kind: str, proc: str = "", title: str = "",
                     shot: str = "", session: int | None = None,
                     extra: dict | None = None, ts: float | None = None,
                     when: str | None = None):
        """
        写一条事件。

        ts / when 可以显式指定时间（补录数据、测试时会用到）。
        关键：显示用的 "t" 必须和数值用的 "ts" 保持一致 ——
        如果只覆盖 ts 而 t 还是当前时刻，统计出来的时间线会全部叠在一起。
        """
        if ts is None:
            ts = time.time()
        if when is None:
            when = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

        rec = {
            "t": when,
            "ts": float(ts),
            "kind": kind,                 # window | session_start | session_end | analyze | note | idle
            "proc": proc,
            "title": title[:300],
            "shot": shot,                 # 相对 SHOT_DIR 的路径
        }
        if session is not None:
            rec["session"] = session
        if extra:
            rec.update(extra)
        try:
            with self._lock, self.events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def read_events(self, day: str | None = None) -> list[dict]:
        path = self._path_read(day, "events", ".jsonl") if day else self.events_file()
        if not path.exists():
            return []
        out = []
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue      # 崩在半行上也不影响其它记录
        except Exception:
            pass
        return out

    # ---------------- 手工段 ----------------
    def read_sessions(self, day: str | None = None) -> list[dict]:
        path = self._path_read(day, "sessions", ".json") if day else self.sessions_file()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def write_sessions(self, sessions: list[dict], day: str | None = None):
        path = self._path_read(day, "sessions", ".json") if day else self.sessions_file()
        try:
            with self._lock:
                path.write_text(json.dumps(sessions, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        except Exception:
            pass

    def start_session(self, task: str = "") -> dict:
        sessions = self.read_sessions()
        idx = len(sessions) + 1
        s = {
            "id": idx,
            "task": task.strip(),
            "start": now_ts(),
            "start_ts": time.time(),
            "end": "",
            "end_ts": 0,
            "seconds": 0,
        }
        sessions.append(s)
        self.write_sessions(sessions)
        self.append_event("session_start", session=idx,
                          extra={"task": s["task"]})
        return s

    def end_session(self) -> dict | None:
        sessions = self.read_sessions()
        open_ones = [s for s in sessions if not s.get("end")]
        if not open_ones:
            return None
        s = open_ones[-1]
        s["end"] = now_ts()
        s["end_ts"] = time.time()
        s["seconds"] = int(s["end_ts"] - s.get("start_ts", s["end_ts"]))
        self.write_sessions(sessions)
        self.append_event("session_end", session=s["id"],
                          extra={"task": s["task"], "seconds": s["seconds"]})
        return s

    def open_session(self) -> dict | None:
        for s in reversed(self.read_sessions()):
            if not s.get("end"):
                return s
        return None

    # ---------------- 未分析的日子 ----------------
    @staticmethod
    def days_with_data() -> list[str]:
        """
        所有有事件记录的日子（升序）。

        用 rglob 递归找，所以新旧两种结构都能看到：
            data/2026-09/events-2026-09-28.jsonl    （新）
            data/events-2026-09-28.jsonl            （旧，升级前留下的）
        归档包（archive-*.zip）不是目录也不是 jsonl，自然不会被匹配到。
        """
        days = set()
        for p in DATA_DIR.rglob("events-*.jsonl"):
            name = p.stem                      # events-2026-10-03
            if name.startswith("events-"):
                days.add(name[len("events-"):])
        return sorted(days)

    @staticmethod
    def pending_days(before: str | None = None) -> list[str]:
        """
        有记录、但还没生成日记的日子（默认只看 before 之前的，即"今天之前"）。

        为什么要看这个：程序是常驻的，你可能忘了点分析就关机了。
        第二天启动时如果不提醒，那天的记录就永远躺在 data/ 里，
        直到被当成「看不见的数据」堆着（截图还会一直占磁盘）。
        """
        out = []
        for d in Store.days_with_data():
            if before and d >= before:
                continue
            events = Store(d).read_events(d)
            has_window = any(e.get("kind") == "window" for e in events)
            if not has_window:
                continue
            if not Store(d).diary_path(d).exists():
                out.append(d)
        return out

    @staticmethod
    def orphans_older_than(days: int) -> list[str]:
        """有记录、没日记、且已经超过 days 天的日子 —— 多半是永远不分析了。"""
        import datetime as _dt
        cutoff = (_dt.date.today() - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
        return [d for d in Store.pending_days() if d < cutoff]

    # ---------------- 分析结果 ----------------
    def read_summary(self, day: str | None = None) -> dict:
        path = self._path_read(day, "summary", ".json") if day else self.summary_file()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def write_summary(self, data: dict, day: str | None = None):
        path = self._path_read(day, "summary", ".json") if day else self.summary_file()
        try:
            with self._lock:
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        except Exception:
            pass

    # ---------------- 日记 ----------------
    #
    # 目录按「月 / 周 / 天」三级组织，一周用 ISO 周号（周一是第一天），
    # 每一天再单独一个文件夹装那天的正文和两张图：
    #
    #   diary/2026-10/2026-W40/
    #   ├── 2026-10-03/
    #   │   ├── diary.md                  每日日记正文
    #   │   ├── chart.png                 当天软件分配
    #   │   └── timeline.png              当天时间线
    #   ├── weekly-2026-W40.md            周报
    #   └── weekly-chart-2026-W40.png     周图
    #
    # 为什么一天一个文件夹：一天会产出 1 篇正文 + 2 张图，一周就是 21 个
    # 文件堆在一个目录里。分开之后「一天的东西」自成一体，删一天、
    # 打包一天、或者整体搬走都不会牵连别的天。
    #
    # 为什么用 ISO 周号而不是 W1/W2：它跨年不会乱（12 月底可能属于明年
    # 的 W01），而且天然可排序、可比较。周号和日期是双向可推导的，
    # 所以不需要额外存映射表。
    #
    # 文件名用固定的 diary/chart/timeline（不带日期）而不是 chart-<日期>.png：
    # 日期已经由两级文件夹表达了，文件名再带一遍是重复。
    @staticmethod
    def week_key(for_day: str) -> str:
        """
        "2026-10-03" -> "2026-W40"（ISO 周，周一为一周开始）。

        注意跨年：2026-12-28 属于 2026-W53（那年有 53 个 ISO 周）；
        而 2029-12-31 属于 2030-W01。月份目录始终按日期本身取，
        所以同一周的日记有可能被分到相邻两个月的目录里 —— 这是刻意的：
        月目录按日期归属，周号只用来做组内分组，不会出现找不到文件的情况。
        """
        try:
            d = date.fromisoformat(for_day)
        except Exception:
            return ""
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"

    @staticmethod
    def week_month(week: str) -> str:
        """
        这一周该归到哪个月（"2029-W01" -> "2030-01"）。

        为什么不能直接用「那天所在的月」：跨月的那一周会被劈开。
        实测 2029-12-31(一) ~ 2030-01-06(日) 这一周 —— 周一是 12 月，
        其余 6 天在 1 月。如果按各天自己的月份放，周一单独躺在
        2029-12/ 里，另外 6 天和周报在 2030-01/ 里，一个「周文件夹」
        被拆成两半。

        规则改成：整周归到**天数占多数**的那个月（打平取周一那个月）。
        上面那周 1 月占 6 天、12 月占 1 天，所以整周都进 2030-01/。
        这样「一周 = 一个文件夹」在任何情况下都成立。
        """
        try:
            y, w = week.split("-W")
            mon = date.fromisocalendar(int(y), int(w), 1)
        except Exception:
            return date.today().strftime("%Y-%m")
        # 数 7 天里各月各占几天
        counts: dict[str, int] = {}
        for i in range(7):
            key = (mon + timedelta(days=i)).strftime("%Y-%m")
            counts[key] = counts.get(key, 0) + 1
        # 取天数最多的月；并列时取周一所在月（dict 顺序即从周一开始数）
        best = max(counts.items(), key=lambda kv: kv[1])
        return best[0]

    def week_dir(self, for_day: str | None = None) -> Path:
        """
        某天所属的「周」目录（周报和该周各天的文件夹都放在这里）。

        注意月份取的是 `week_month()` 的结果，而不是这一天自己的月 ——
        这样跨月的那一周不会两边分家。调用方无论传周一还是周日，
        得到的都是同一个目录。
        """
        d = for_day or self.day
        wk = self.week_key(d) or "unknown-week"
        folder = DIARY_DIR / self.week_month(wk) / wk
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def diary_dir(self, for_day: str | None = None) -> Path:
        """某天自己的目录（放那天的正文和两张图）。"""
        d = for_day or self.day
        folder = self.week_dir(d) / d
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def diary_path(self, for_day: str | None = None) -> Path:
        d = for_day or self.day
        return self.diary_dir(d) / "diary.md"

    def weekly_path(self, week: str, for_day: str | None = None) -> Path:
        """周报路径。for_day 用来决定放哪个月目录（不传就按该周周一）。"""
        d = for_day or self._week_monday(week)
        return self.week_dir(d) / f"weekly-{week}.md"

    @staticmethod
    def _week_monday(week: str) -> str:
        """'2026-W40' -> 该周周一的日期。用于定位月目录。"""
        try:
            y, w = week.split("-W")
            return date.fromisocalendar(int(y), int(w), 1).isoformat()
        except Exception:
            return date.today().isoformat()

    def write_diary(self, text: str, for_day: str | None = None) -> Path:
        p = self.diary_path(for_day)
        p.write_text(text, encoding="utf-8")
        return p

    def write_weekly(self, text: str, week: str, for_day: str | None = None) -> Path:
        p = self.weekly_path(week, for_day)
        p.write_text(text, encoding="utf-8")
        return p

    def read_diary(self, for_day: str | None = None) -> str:
        p = self.diary_path(for_day)
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def list_diaries(self) -> list[Path]:
        """所有日报（不含周报），按日期降序。"""
        if not DIARY_DIR.exists():
            return []
        out = [p for p in DIARY_DIR.rglob("*.md")
               if not p.name.startswith("weekly-")]
        return sorted(out, reverse=True)

    def list_weeklies(self) -> list[Path]:
        """所有周报，按周号降序。"""
        if not DIARY_DIR.exists():
            return []
        return sorted((p for p in DIARY_DIR.rglob("weekly-*.md")), reverse=True)

    # ---------------- 目录迁移 ----------------
    @staticmethod
    def _looks_like_week_dir(name: str) -> bool:
        """目录名是不是周号（形如 2026-W40）。"""
        parts = name.split("-W")
        return len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit()

    def migrate_diary_layout(self, logger=None) -> dict:
        """
        把旧结构挪到新结构。要处理两种旧布局：

          1) diary/年-月/日期.md
             最早的结构，日记正文直接躺在月目录里。
          2) diary/年-月/年-W周/日期.md
             中间版本，加上周目录但一天的东西还是平铺的。

        目标：
          diary/年-月/年-W周/日期/diary.md     （正文）
          diary/年-月/年-W周/日期/chart.png    （图，跟着正文搬）

        为什么要迁移而不是让旧文件留在原地：日记是长期资产，用户会翻旧账。
        如果新旧几套结构并存，以后写「列出所有日记」「按月统计」这类功能
        就得兼容所有路径，越拖越麻烦。

        安全措施：
          * 只搬能识别出日期的 .md，其它文件（比如手写的笔记）一律不动
          * 目标已存在时**不覆盖**，把待搬的文件改名成 xxx.old.md 留着
          * 图跟着正文走，且只在目标没有同名图时才搬
          * 任何单个文件出错都跳过，不中断整体迁移，最后清掉空的周目录
        返回 {"moved": n, "pics": n, "skipped": n, "conflicts": [..]}
        """
        log = logger or (lambda *a, **k: None)
        result = {"moved": 0, "pics": 0, "skipped": 0, "conflicts": []}
        if not DIARY_DIR.exists():
            return result

        for p in sorted(DIARY_DIR.rglob("*.md")):
            if p.name.startswith("weekly-"):
                continue
            # 文件名必须是 <日期>.md（新结构的 diary.md 也允许，靠父目录认日期）
            if p.stem == "diary" and p.parent.name.count("-") == 2:
                continue                     # 已经是 diary/<日期>/diary.md
            try:
                day = date.fromisoformat(p.stem).isoformat()
            except Exception:
                result["skipped"] += 1
                continue
            parent_is_week = self._looks_like_week_dir(p.parent.name)
            # 已经在新结构（父目录是周号，且文件在 <日期>/ 里）
            if parent_is_week and p.stem != day:
                continue

            target = self.diary_path(day)
            if target == p:
                continue
            if target.exists():
                alt = target.with_name("diary.old.md")
                if alt.exists():
                    result["skipped"] += 1
                    continue
                try:
                    p.replace(alt)
                    result["conflicts"].append(str(alt.relative_to(DIARY_DIR)))
                    result["moved"] += 1
                except Exception:
                    result["skipped"] += 1
                continue

            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                p.replace(target)
                result["moved"] += 1
            except Exception as e:
                log(f"[迁移] {p.name} 失败：{e}")
                result["skipped"] += 1
                continue

            # 把这一天的图也搬过去（chart-/timeline- 两种前缀）
            for pat in (f"chart-{day}.png", f"timeline-{day}.png"):
                src_img = p.parent / pat
                if not src_img.exists():
                    continue
                new_name = pat.split("-")[0] + ".png"     # chart.png / timeline.png
                dst_img = target.parent / new_name
                if dst_img.exists():
                    continue
                try:
                    src_img.replace(dst_img)
                    result["pics"] += 1
                except Exception:
                    pass

        # 清掉搬空了的目录（先周后月；只删空的，删不掉就算了）
        try:
            for depth in (2, 1):
                for d in sorted(DIARY_DIR.glob("/".join(["*"] * depth)),
                                key=lambda p: len(p.parts), reverse=True):
                    if d.is_dir() and not any(d.iterdir()):
                        d.rmdir()
        except Exception:
            pass

        if result["moved"]:
            log(f"[迁移] 日记目录结构已更新，搬动 {result['moved']} 篇正文"
                f"、{result['pics']} 张图")
        return result

    # ---------------- 月度归档 ----------------
    @staticmethod
    def month_dir(month: str) -> Path:
        """某个月的数据目录（形如 data/2026-09）。"""
        return DATA_DIR / month

    @staticmethod
    def archive_path(month: str) -> Path:
        return DATA_DIR / month / f"archive-{month}.zip"

    @staticmethod
    def needs_archive(today: str | None = None) -> str:
        """
        今天该不该归档，该归档哪个月。返回月份字符串或空串。

        规则：每月 **7 号及以后**，归档**上一个月**。

        为什么定在 7 号而不是 1 号：你可能会在月初补分析上个月最后几天
        （截图保留 7 天，所以月初补得上）。1 号就归档会把还在用的目录包起来；
        留 7 天缓冲更稳。归档是幂等的，7 号之后每天启动都会检查，已经打过包
        就跳过。
        """
        d = today or today_str()
        try:
            dt = date.fromisoformat(d)
        except Exception:
            return ""
        if dt.day < ARCHIVE_AFTER_DAY:
            return ""
        # 上一个月
        first = dt.replace(day=1)
        prev = first - timedelta(days=1)
        return prev.strftime("%Y-%m")

    @staticmethod
    def make_month_archive(month: str, logger=None) -> Path | None:
        """
        把某个月的数据目录打包成 archive-<月>.zip。

        **保留原目录**，只多出一个压缩包。

        为什么不删原目录：一年才 1.4 MB，删掉省的这点空间远不如「随时能
        打开看原始记录」值钱。而且真删了的话，从压缩包里读数据这条路
        我还没写（也不想写 —— 读路径每处都得分支处理 zip，很容易出错）。
        所以归档是**纯增量**的：包留着当存档，目录留着当活数据。
        """
        import zipfile
        log = logger or (lambda *a, **k: None)
        src = Store.month_dir(month)
        if not src.is_dir():
            return None
        files = [p for p in sorted(src.iterdir())
                 if p.is_file() and p.suffix in (".jsonl", ".json")]
        if not files:
            return None                    # 这个月没数据，不用建空包

        out = Store.archive_path(month)
        tmp = out.with_suffix(".zip.part")
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                for p in files:
                    z.write(p, p.name)
            tmp.replace(out)               # 原子替换，避免半个包
            log(f"[归档] {month} 已打包 {len(files)} 个文件 -> {out.name}")
            return out
        except Exception as e:
            log(f"[归档] {month} 失败：{e}")
            try:
                tmp.unlink()
            except Exception:
                pass
            return None

    @staticmethod
    def archive_due_months(today: str | None = None, logger=None) -> list[str]:
        """
        归档所有「该归档但还没归档」的月份（不只上个月）。

        为什么要遍历所有月份而不只看上个月：如果你一个月没开程序，
        上上个月就漏掉了。这里按目录扫一遍，每个没有 archive-*.zip 的
        历史月份都补一个。
        """
        log = logger or (lambda *a, **k: None)
        due = Store.needs_archive(today)
        if not due:
            return []
        done = []
        try:
            months = sorted(p.name for p in DATA_DIR.iterdir()
                            if p.is_dir() and len(p.name) == 7
                            and p.name[4] == "-" and p.name[:4].isdigit())
        except Exception:
            return []
        for m in months:
            if m > due:                    # 当月和未来的月份不归档
                continue
            if Store.archive_path(m).exists():
                continue
            if Store.make_month_archive(m, logger=log):
                done.append(m)
        return done

    # ---------------- 旧数据搬迁 ----------------
    @staticmethod
    def migrate_data_layout(logger=None) -> int:
        """
        把 data/ 根下的旧文件搬进「年月」子目录。

        和日记那次迁移同理：旧结构留着的话，读路径得永远兼容两套位置。
        读取的兼容分支（_path_read）保留着当保险，但正常走一遍迁移之后
        就不会再命中它了。

        只搬文件名能解析出日期的文件，别的不动。
        """
        log = logger or (lambda *a, **k: None)
        moved = 0
        for kind, ext in (("events", ".jsonl"), ("sessions", ".json"),
                          ("summary", ".json")):
            for p in DATA_DIR.glob(f"{kind}-*{ext}"):
                day = p.stem[len(kind) + 1:]
                try:
                    date.fromisoformat(day)
                except Exception:
                    continue
                target = Store._path(day, kind, ext)
                if target.exists() or target == p:
                    continue
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    p.replace(target)
                    moved += 1
                except Exception as e:
                    log(f"[搬迁] {p.name} 失败：{e}")
        if moved:
            log(f"[搬迁] data 目录已按年月整理，搬动 {moved} 个文件")
        return moved

    # ---------------- 清理 ----------------
    def clear_day(self):
        """把某天的分析痕迹清掉，方便重跑。"""
        for p in (self.summary_path,):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass
        try:
            for f in self.shot_dir().glob("*.jpg"):
                f.unlink()
        except Exception:
            pass

    def purge_day(self):
        """
        删掉某一天的**全部**数据文件（事件流 + 手动段 + 分析结果）。

        给测试用的：测试要造很多天的假数据，跑完得清干净。
        以前测试自己在外面拼 `DATA_DIR / f"events-{day}.jsonl"` 来删 ——
        目录结构改成「年月」之后那种拼法就删错位置了（删不掉、也不报错，
        于是假数据在一次运行里悄悄累积）。所以清理入口收到这里来，
        和写入用同一套路径解析。
        """
        for kind, ext in (("events", ".jsonl"), ("sessions", ".json"),
                          ("summary", ".json")):
            for p in {self._path(self.day, kind, ext),
                      DATA_DIR / f"{kind}-{self.day}{ext}"}:
                try:
                    if p.exists():
                        p.unlink()
                except Exception:
                    pass

    def shot_dir(self) -> Path:
        d = SHOT_DIR / self.day
        d.mkdir(parents=True, exist_ok=True)
        return d
