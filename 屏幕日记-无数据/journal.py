"""
屏幕日记 —— 桌面界面

三个主按钮对应三件事：
  开始记录      开一段手动记录（可顺手写「这段要干什么」）
  结束并统计    关掉这段，立刻算时长和软件分配
  我有空，开始分析   你不碰电脑了 -> 批量读图 -> 生成今天的日记

界面里的耗时活全在后台线程跑，主线程只刷界面，不会卡。
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

import config
from analysis import compute, fmt_duration, plan_review
from collector import Collector
from store import Store, today_str

# 中文常见字体，按可用性挑一个
UI_FONT = "Microsoft YaHei UI"

C_BG = "#f7f8fa"
C_CARD = "#ffffff"
C_TEXT = "#1a202c"
C_MUTED = "#8c94a0"
C_GREEN = "#38a169"
C_RED = "#e53e3e"
C_BLUE = "#2b6cb0"
C_AMBER = "#d69e2e"


def enable_dpi_awareness():
    """让 tkinter 和截图在缩放屏幕上坐标一致。"""
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ================================================================
#  补分析窗口
# ================================================================
class BatchAnalyzeDialog(tk.Toplevel):
    """
    勾选要补分析的日子，然后批量跑。

    为什么要有它：以前只在启动时弹一个 yes/no，多个未分析的日子只能选
    「最近一天」，缺 4 天就得开 4 次程序。而且分析过的日子没法重跑 ——
    想看新计划下的结果、或者模型更新后想重算，都没有入口。

    每一行都显示**事件数**和**截图数**：有没有截图直接决定那天日记的质量
    （有图 → 有屏幕印象；没图 → 只有时长统计）。不显示的话，你没法理解
    为什么某天的日记特别干瘪。
    """

    def __init__(self, parent, days: list[dict], on_start, allow_rerun=True):
        super().__init__(parent)
        self.days = days
        self.on_start = on_start
        self.title("补分析")
        self.configure(bg=C_BG)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.vars: list[tk.BooleanVar] = []
        self._build(allow_rerun)
        self._recalc()

        self.update_idletasks()
        # 居中到父窗口
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            w, h = self.winfo_width(), self.winfo_height()
            self.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 3}")
        except Exception:
            pass

    def _build(self, allow_rerun):
        wrap = tk.Frame(self, bg=C_BG)
        wrap.pack(fill="both", expand=True, padx=16, pady=14)

        tk.Label(wrap, text="选择要分析的日子", bg=C_BG, fg=C_TEXT,
                 font=(UI_FONT, 13, "bold")).pack(anchor="w")
        tk.Label(wrap,
                 text=("带勾的日子会依次生成日记。没有截图的日子也能补，\n"
                       "那种情况只按窗口标题统计时长，日记里不会有屏幕印象。"),
                 bg=C_BG, fg=C_MUTED, font=(UI_FONT, 9),
                 justify="left").pack(anchor="w", pady=(2, 10))

        box = tk.Frame(wrap, bg=C_CARD, highlightbackground="#e4e8f0",
                       highlightthickness=1)
        box.pack(fill="both", expand=True)

        self.list_inner = tk.Frame(box, bg=C_CARD)
        self.list_inner.pack(fill="both", expand=True, padx=10, pady=8)

        for d in self.days:
            var = tk.BooleanVar(value=not d.get("done"))
            self.vars.append(var)
            row = tk.Frame(self.list_inner, bg=C_CARD)
            row.pack(fill="x", pady=2)
            cb = tk.Checkbutton(
                row, variable=var, bg=C_CARD, activebackground=C_CARD,
                command=self._recalc, text="")
            cb.pack(side="left")
            tag = "✓ 已生成" if d.get("done") else "未生成"
            color = C_MUTED if d.get("done") else C_TEXT
            tk.Label(row, text=f"{d['day']}  周{d.get('wd','')}   {tag}",
                     bg=C_CARD, fg=color, font=(UI_FONT, 10),
                     anchor="w", width=22).pack(side="left")
            tk.Label(row,
                     text=f"{d['events']:3d} 条事件   {d['shots']:3d} 张截图",
                     bg=C_CARD, fg=C_MUTED, font=(UI_FONT, 9),
                     anchor="w").pack(side="left")
            if d.get("note"):
                tk.Label(row, text=d["note"], bg=C_CARD, fg=C_AMBER,
                         font=(UI_FONT, 9)).pack(side="left", padx=(8, 0))

        btns = tk.Frame(wrap, bg=C_BG)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="全选", width=8,
                   command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(btns, text="全不选", width=8,
                   command=lambda: self._set_all(False)).pack(side="left",
                                                              padx=(6, 0))
        self.est = tk.Label(btns, text="", bg=C_BG, fg=C_MUTED,
                            font=(UI_FONT, 9))
        self.est.pack(side="left", padx=(12, 0))

        self.warn = tk.Label(wrap, text="", bg=C_BG, fg=C_AMBER,
                             font=(UI_FONT, 9), justify="left", anchor="w",
                             wraplength=460)
        self.warn.pack(fill="x", pady=(6, 0))

        tail = tk.Frame(wrap, bg=C_BG)
        tail.pack(fill="x", pady=(10, 0))
        ttk.Button(tail, text="取消", command=self.destroy
                   ).pack(side="right")
        self.btn_go = ttk.Button(tail, text="开始补分析",
                                 command=self._go)
        self.btn_go.pack(side="right", padx=(0, 8))

    def _set_all(self, val: bool):
        for v in self.vars:
            v.set(val)
        self._recalc()

    def _selected(self) -> list[dict]:
        return [d for d, v in zip(self.days, self.vars) if v.get()]

    def _recalc(self):
        sel = self._selected()
        n_shot = sum(d["shots"] for d in sel)
        # 视觉分析每张约 1.2 秒；没截图的日子只需几秒统计+写总结
        est = n_shot * config.VISION_IMAGE_SECONDS + len(sel) * 6
        if not sel:
            self.est.configure(text="没有选中任何日子")
        elif est < 60:
            self.est.configure(text=f"已选 {len(sel)} 天 · "
                                    f"{n_shot} 张截图 · 预计约 {est:.0f} 秒")
        else:
            self.est.configure(text=f"已选 {len(sel)} 天 · "
                                    f"{n_shot} 张截图 · 预计约 {est/60:.1f} 分钟")

        # 重跑已有日记的警告（按你的选择：A 留备份 + B 提示）
        rerun = [d for d in sel if d.get("done")]
        no_shot = [d for d in sel if not d["shots"]]
        msgs = []
        if rerun:
            msgs.append(f"其中 {len(rerun)} 天已有日记，会被重新生成"
                        f"（旧日记另存为 diary.old.md）")
        if no_shot:
            msgs.append(f"{len(no_shot)} 天没有截图，日记里不会有屏幕印象")
        if est > 20 * 60:
            msgs.append("⚠️ 预计超过 20 分钟，建议你先确认真的不用电脑")
        self.warn.configure(text="\n".join(msgs))

    def _go(self):
        sel = self._selected()
        if not sel:
            messagebox.showinfo("没有选中", "先勾选至少一天。", parent=self)
            return
        self.destroy()
        self.on_start(sel)


# ================================================================
#  日记浏览窗口
# ================================================================
class DiaryBrowser(tk.Toplevel):
    """
    按「月 / 周 / 天」翻看已生成的日记和周报。

    为什么需要它：日记按 diary/年-月/年-W周/日期/ 存成三层目录，一周就有
    23 个文件。主界面只有「看今天的日记」，想看前几天的就得自己去资源
    管理器里翻 —— 有了周报之后更明显：一周 7 篇正文 + 14 张图 + 周报，
    路径记不住。

    结构用 Treeview 三级（月 -> 周 -> 天），和磁盘上的目录一一对应，
    这样「界面上看到的」和「文件夹里看到的」是同一套结构，不会让人困惑。
    选中一项时右边显示那天/那周的关键数字，双击打开正文。
    """

    def __init__(self, parent: tk.Tk, store: Store, today: str = ""):
        super().__init__(parent)
        self.store = store
        self.today = today or today_str()
        self.title("日记浏览")
        self.geometry("820x600")
        self.minsize(680, 460)
        self.configure(bg=C_BG)
        self.transient(parent)

        self._items: dict[str, Path] = {}      # tree item id -> 正文路径
        self._build()
        self.refresh()

        # 打开就定位到今天（如果有），省得自己找
        self.after(60, self._focus_today)

    # ---------------- 界面 ----------------
    def _build(self):
        head = tk.Frame(self, bg=C_BG)
        head.pack(fill="x", padx=12, pady=(12, 6))
        tk.Label(head, text="日记浏览", bg=C_BG, fg=C_TEXT,
                 font=(UI_FONT, 14, "bold")).pack(side="left")
        self.count_label = tk.Label(head, text="", bg=C_BG, fg=C_MUTED,
                                    font=(UI_FONT, 9))
        self.count_label.pack(side="left", padx=(10, 0))
        ttk.Button(head, text="刷新", command=self.refresh).pack(side="right")
        ttk.Button(head, text="打开日记目录",
                   command=self._open_root).pack(side="right", padx=(0, 6))

        body = tk.Frame(self, bg=C_BG)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        # ---- 左：树 ----
        left = tk.Frame(body, bg=C_CARD, highlightbackground="#e4e8f0",
                        highlightthickness=1)
        left.pack(side="left", fill="both", expand=True)

        style = ttk.Style(self)
        try:
            style.configure("Browser.Treeview", font=(UI_FONT, 10), rowheight=26)
            style.configure("Browser.Treeview.Heading", font=(UI_FONT, 10))
        except Exception:
            pass

        self.tree = ttk.Treeview(left, style="Browser.Treeview",
                                 columns=("info",), show="tree headings",
                                 selectmode="browse")
        self.tree.heading("#0", text="日期", anchor="w")
        self.tree.heading("info", text="活跃 / 计划", anchor="w")
        self.tree.column("#0", width=260, minwidth=180, stretch=False)
        self.tree.column("info", width=210, minwidth=120, anchor="w")

        vs = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        vs.pack(side="right", fill="y", pady=6, padx=(0, 6))

        self.tree.tag_configure("month", foreground=C_MUTED)
        self.tree.tag_configure("week", foreground=C_TEXT)
        self.tree.tag_configure("today", foreground=C_GREEN)
        self.tree.tag_configure("weekly", foreground=C_BLUE)

        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())
        self.tree.bind("<Double-1>", lambda e: self.open_diary())
        self.tree.bind("<Return>", lambda e: self.open_diary())

        # ---- 右：详情 ----
        right = tk.Frame(body, bg=C_CARD, width=250,
                         highlightbackground="#e4e8f0", highlightthickness=1)
        right.pack(side="left", fill="y", padx=(10, 0))
        right.pack_propagate(False)

        inner = tk.Frame(right, bg=C_CARD)
        inner.pack(fill="both", expand=True, padx=12, pady=12)
        self.detail_title = tk.Label(inner, text="选一项看看", bg=C_CARD,
                                     fg=C_TEXT, font=(UI_FONT, 12, "bold"),
                                     anchor="w", justify="left", wraplength=210)
        self.detail_title.pack(fill="x")
        self.detail_body = tk.Label(inner, text="", bg=C_CARD, fg=C_MUTED,
                                    font=(UI_FONT, 9), anchor="w",
                                    justify="left", wraplength=210)
        self.detail_body.pack(fill="x", pady=(8, 0))

        tk.Frame(inner, bg="#e4e8f0", height=1).pack(fill="x", pady=10)
        self.file_box = tk.Frame(inner, bg=C_CARD)
        self.file_box.pack(fill="both", expand=True)

        ttk.Button(inner, text="打开正文", command=self.open_diary
                   ).pack(fill="x", pady=(10, 4))
        ttk.Button(inner, text="用资源管理器打开所在文件夹",
                   command=self._reveal).pack(fill="x")

    # ---------------- 数据 ----------------
    def refresh(self):
        """重建整棵树。日记量不大（一年 365 项），直接重建最省心。"""
        self.tree.delete(*self.tree.get_children())
        self._items.clear()

        entries = self._collect()
        if not entries:
            self.count_label.configure(text="还没有任何日记")
            return

        # 按「月 -> 周」分组，月和周都按倒序（最近的在上）
        months: dict[str, dict[str, list[dict]]] = {}
        for e in entries:
            months.setdefault(e["month"], {}).setdefault(e["week"], []).append(e)

        n_days = n_weeks = 0
        for month in sorted(months, reverse=True):
            weeks = months[month]
            m_item = self.tree.insert(
                "", "end", text=f"{month}",
                values=(f"{sum(len(v) for v in weeks.values())} 项",),
                open=(month == entries[0]["month"]), tags=("month",))
            for wk in sorted(weeks, reverse=True):
                # 显示顺序：天按日期倒序（最新的在上），周报接在最后一行。
                #
                # 这个顺序我改错了三次，把结论写清楚免得再犯：
                #   1) 拼字符串 "2026-09-28+7" 再整体倒序 —— "+"(0x2B) 比
                #      "-"(0x2D) 小，周报被顶到最前；
                #   2) 元组 (日期, 序号) 整体 reverse=True —— 序号也倒序，
                #      seq=7 的周报又跑到第一行；
                #   3) 想靠 sorted 的稳定性「保持 _collect 的日期倒序」——
                #      不成立：各天的 seq 互不相同，压根不是同一个键，
                #      稳定排序那条规则用不上。
                # 别绕了：天自己按日期倒序排，周报单独接在末尾。
                day_rows = sorted((r for r in weeks[wk] if r["kind"] == "day"),
                                  key=lambda r: r["day"], reverse=True)
                rows = day_rows + [r for r in weeks[wk] if r["kind"] != "day"]
                wk_title = next((r["week_label"] for r in rows if r["week_label"]), wk)
                w_item = self.tree.insert(
                    m_item, "end", text=f"{wk}  ({wk_title})",
                    values=("",), open=True, tags=("week",))
                n_weeks += 1
                for r in rows:
                    tag = ("weekly",) if r["kind"] == "weekly" else ()
                    if r["kind"] == "day" and r["day"] == self.today:
                        tag = ("today",)
                    iid = self.tree.insert(
                        w_item, "end", text=r["label"],
                        values=(r["info"],), tags=tag)
                    self._items[iid] = r["path"]
                    if r["kind"] == "day":
                        n_days += 1

        self.count_label.configure(
            text=f"{n_days} 篇日记 · {n_weeks} 周")

    def _collect(self) -> list[dict]:
        """
        收集所有日报和周报，附带能一眼看懂的摘要。

        摘要直接读 summary-<day>.json（分析时写的），不再重算 ——
        重算要读事件流，翻个列表就把 30 天的事件全读一遍太浪费。
        老数据可能没有 summary，那就只显示日期，不显示数字。
        """
        import weekly as W

        out: list[dict] = []

        for p in self.store.list_diaries():
            day = p.parent.name
            try:
                date.fromisoformat(day)
            except Exception:
                continue
            wk = self.store.week_key(day)
            month = (self.store.week_month(wk) if hasattr(self.store, "week_month")
                     else day[:7])
            snap = self.store.read_summary(day) or {}
            active = 0
            for a in snap.get("apps", []) or []:
                active += int(a.get("seconds", 0) or 0)
            rev = snap.get("review") or {}
            info_bits = []
            if active:
                info_bits.append(fmt_duration(active))
            if rev.get("total"):
                info_bits.append(f"{rev.get('done', 0)}/{rev['total']}")
            try:
                wd = "一二三四五六日"[date.fromisoformat(day).weekday()]
            except Exception:
                wd = ""
            # 排序用 (周一日期, 周内序号)：天是 0..6（周一→周日），
            # 周报给 7，于是它落在那周所有天之后。
            #
            # 为什么不拼成一个字符串排序：曾经拼成 "2026-09-28+7"，
            # 而 "+"(0x2B) 比 "-"(0x2D) 小，整体倒序时周报会被顶到最前面。
            # 拆成元组让比较按字段走，就不会有这种坑。
            this_monday = W._monday_of(wk)
            dow = (date.fromisoformat(day) - date.fromisoformat(this_monday)).days
            out.append({
                "kind": "day", "day": day, "month": month, "week": wk,
                "week_label": W.week_range_label(wk),
                "monday": this_monday, "seq": dow, "month_key": month,
                "label": f"{day[5:]}  周{wd}",
                "info": "  ".join(info_bits), "path": p,
            })

        for p in self.store.list_weeklies():
            wk = p.stem.replace("weekly-", "")
            month = (self.store.week_month(wk) if hasattr(self.store, "week_month")
                     else p.parent.parent.name)
            info = self._weekly_info(wk)
            # 排序键用该周的**周一日期**，和周内各天的键（也是日期）同一种
            # 格式才能正确排到一起。曾经用过 "周号~9"，结果 "2026-W40~9" 和
            # "2026-10-04" 比较时 "1" < "W"，周报反而排到了所有天前面。
            try:
                monday = W._monday_of(wk)
            except Exception:
                monday = wk
            out.append({
                "kind": "weekly", "day": "", "month": month, "week": wk,
                "week_label": "", "monday": monday, "seq": 7, "month_key": month,
                "label": f"周报 {wk}", "info": info, "path": p,
            })

        return out

    def _weekly_info(self, week: str) -> str:
        """周报的摘要：直接读那一周各天的 summary 加总，不重算聚合。"""
        import weekly as W
        try:
            days = W.week_days(W._monday_of(week))
        except Exception:
            return ""
        total = done = tot = 0
        n = 0
        for d in days:
            snap = self.store.read_summary(d) or {}
            if not snap:
                continue
            n += 1
            for a in snap.get("apps", []) or []:
                total += int(a.get("seconds", 0) or 0)
            rev = snap.get("review") or {}
            if rev.get("total"):
                done += int(rev.get("done", 0) or 0)
                tot += int(rev["total"])
        bits = []
        if total:
            bits.append(f"合计 {fmt_duration(total)}")
        if tot:
            bits.append(f"计划 {done}/{tot}")
        if n:
            bits.append(f"{n}/7 天")
        return "  ".join(bits)

    # ---------------- 交互 ----------------
    def _selected_path(self) -> Path | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return self._items.get(sel[0])

    def _on_select(self):
        sel = self.tree.selection()
        for w in self.file_box.winfo_children():
            w.destroy()
        if not sel:
            return
        iid = sel[0]
        p = self._items.get(iid)
        label = self.tree.item(iid, "text")
        if not p:
            # 选中的是月或周节点，只显示一行说明
            self.detail_title.configure(text=label.strip())
            self.detail_body.configure(
                text=f"{self.tree.item(iid, 'values')[0]}\n\n展开后选某一天或周报。")
            return

        self.detail_title.configure(text=label.strip())
        bits = [self.tree.item(iid, "values")[0] or "—"]
        try:
            bits.append(f"文件 {p.stat().st_size / 1024:.1f} KB")
        except Exception:
            pass
        self.detail_body.configure(text="\n".join(b for b in bits if b))

        # 同目录的文件（正文 + 两张图）
        try:
            files = sorted(x for x in p.parent.iterdir() if x.is_file())
        except Exception:
            files = [p]
        tk.Label(self.file_box, text="同目录文件", bg=C_CARD, fg=C_TEXT,
                 font=(UI_FONT, 9, "bold"), anchor="w").pack(fill="x")
        for f in files:
            row = tk.Frame(self.file_box, bg=C_CARD)
            row.pack(fill="x", pady=1)
            is_doc = f.suffix.lower() == ".md"
            lbl = tk.Label(row, text=("📄 " if is_doc else "🖼 ") + f.name,
                           bg=C_CARD, fg=C_BLUE if is_doc else C_MUTED,
                           font=(UI_FONT, 9), anchor="w", cursor="hand2")
            lbl.pack(side="left")
            lbl.bind("<Button-1>", lambda e, ff=f: self._open_file(ff))

    def _open_file(self, p: Path):
        try:
            os.startfile(str(p))
        except Exception:
            try:
                subprocess.Popen(["notepad.exe", str(p)])
            except Exception:
                pass

    def open_diary(self):
        p = self._selected_path()
        if p and p.exists():
            self._open_file(p)

    def _reveal(self):
        p = self._selected_path()
        if not p:
            return
        try:
            subprocess.Popen(["explorer.exe", "/select,", str(p)])
        except Exception:
            self._open_root()

    def _open_root(self):
        try:
            os.startfile(str(config.DIARY_DIR))
        except Exception:
            pass

    def _focus_today(self):
        """打开窗口时定位到今天那一项。"""
        for iid, p in self._items.items():
            if p.parent.name == self.today:
                self.tree.selection_set(iid)
                self.tree.see(iid)
                return


class JournalApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.day = today_str()
        self.store = Store(self.day)
        self.collector = Collector(logger=self.log)
        self.msg_q: queue.Queue = queue.Queue()
        self.busy = False
        self.stop_requested = False
        self.last_result = None
        self.analyzing_day = ""          # 正在分析哪一天（补分析时不是今天）
        self.plan_done_vars: dict[str, tk.BooleanVar] = {}

        self._build()
        self._tick()
        self._drain_queue()

    # ============================================================
    #  界面
    # ============================================================
    def _build(self):
        r = self.root
        r.title("屏幕日记")
        r.configure(bg=C_BG)
        r.geometry("520x720")
        r.minsize(460, 600)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TButton", font=(UI_FONT, 11), padding=6)
        style.configure("Big.TButton", font=(UI_FONT, 12, "bold"), padding=10)
        style.configure("TLabel", background=C_BG, foreground=C_TEXT,
                        font=(UI_FONT, 10))
        style.configure("Card.TLabel", background=C_CARD, foreground=C_TEXT,
                        font=(UI_FONT, 10))
        style.configure("Muted.TLabel", background=C_BG, foreground=C_MUTED,
                        font=(UI_FONT, 9))
        style.configure("Head.TLabel", background=C_BG, foreground=C_TEXT,
                        font=(UI_FONT, 13, "bold"))

        outer = tk.Frame(r, bg=C_BG)
        outer.pack(fill="both", expand=True, padx=14, pady=12)

        # ---------- 顶部状态 ----------
        head = tk.Frame(outer, bg=C_BG)
        head.pack(fill="x")
        tk.Label(head, text="屏幕日记", bg=C_BG, fg=C_TEXT,
                 font=(UI_FONT, 17, "bold")).pack(side="left")

        # 「设置」放最右边，次要功能都收在这里。
        #
        # 为什么收起来：主界面上原来并排摆着「浏览日记 / 设置计划 / 本周周报」
        # 等五六个按钮，常用的其实只有「开始分析」。按钮越多越难一眼找到
        # 主要动作，所以把「偶尔用一次」的都挪进这个菜单。
        self.btn_settings = ttk.Button(head, text="设置 ▾",
                                       command=self.on_settings_menu,
                                       width=9)
        self.btn_settings.pack(side="right")

        self.status_dot = tk.Label(head, text="●", bg=C_BG, fg=C_MUTED,
                                  font=(UI_FONT, 15))
        self.status_dot.pack(side="right", padx=(6, 8))
        self.status_text = tk.Label(head, text="未开始", bg=C_BG, fg=C_MUTED,
                                    font=(UI_FONT, 11))
        self.status_text.pack(side="right")

        # 专注模式开关，放在标题下面一行 —— 它会影响「这段算不算在用」，
        # 是个你随时要能看见状态的东西，所以不塞进设置菜单里。
        focus_row = tk.Frame(outer, bg=C_BG)
        focus_row.pack(fill="x", pady=(2, 0))
        self.focus_var = tk.BooleanVar(value=False)
        self.focus_check = tk.Checkbutton(
            focus_row, text="专注模式（看网课 / 思考时用）",
            variable=self.focus_var, command=self.on_toggle_focus,
            bg=C_BG, activebackground=C_BG, fg=C_TEXT,
            selectcolor=C_CARD, font=(UI_FONT, 9),
            anchor="w", highlightthickness=0, bd=0)
        self.focus_check.pack(side="left")
        self.focus_hint = tk.Label(
            focus_row, text="", bg=C_BG, fg=C_AMBER,
            font=(UI_FONT, 9), anchor="w")
        self.focus_hint.pack(side="left", padx=(10, 0))

        self.status_detail = tk.Label(outer, text="", bg=C_BG, fg=C_MUTED,
                                     font=(UI_FONT, 9), anchor="w",
                                     justify="left", wraplength=480)
        self.status_detail.pack(fill="x", pady=(4, 10))

        # ---------- 手动记录卡片 ----------
        card1 = tk.Frame(outer, bg=C_CARD, highlightbackground="#e4e8f0",
                         highlightthickness=1)
        card1.pack(fill="x", pady=(0, 10))
        inner1 = tk.Frame(card1, bg=C_CARD)
        inner1.pack(fill="x", padx=14, pady=12)

        tk.Label(inner1, text="手动记录", bg=C_CARD, fg=C_TEXT,
                 font=(UI_FONT, 12, "bold")).pack(anchor="w")
        self.session_label = tk.Label(
            inner1, text="没有进行中的记录", bg=C_CARD, fg=C_MUTED,
            font=(UI_FONT, 10), anchor="w", justify="left", wraplength=440)
        self.session_label.pack(fill="x", pady=(4, 10))

        row1 = tk.Frame(inner1, bg=C_CARD)
        row1.pack(fill="x")
        self.btn_start = ttk.Button(row1, text="开始记录", style="Big.TButton",
                                    command=self.on_start)
        self.btn_start.pack(side="left", expand=True, fill="x", padx=(0, 6))
        self.btn_end = ttk.Button(row1, text="结束并统计", style="Big.TButton",
                                  command=self.on_end, state="disabled")
        self.btn_end.pack(side="left", expand=True, fill="x", padx=(6, 0))

        # ---------- 分析卡片 ----------
        card2 = tk.Frame(outer, bg=C_CARD, highlightbackground="#e4e8f0",
                         highlightthickness=1)
        card2.pack(fill="x", pady=(0, 10))
        inner2 = tk.Frame(card2, bg=C_CARD)
        inner2.pack(fill="x", padx=14, pady=12)

        tk.Label(inner2, text="生成日记", bg=C_CARD, fg=C_TEXT,
                 font=(UI_FONT, 12, "bold")).pack(anchor="w")
        tk.Label(inner2, text="你要离开电脑了？按一下，让它安心读图写日记。",
                 bg=C_CARD, fg=C_MUTED, font=(UI_FONT, 9),
                 anchor="w").pack(fill="x", pady=(2, 8))

        self.btn_analyze = ttk.Button(inner2, text="我有空，开始分析",
                                      style="Big.TButton", command=self.on_analyze)
        self.btn_analyze.pack(fill="x")

        self.progress = ttk.Progressbar(inner2, mode="determinate",
                                        maximum=100)
        self.progress.pack(fill="x", pady=(10, 2))
        self.progress_label = tk.Label(inner2, text="", bg=C_CARD, fg=C_MUTED,
                                       font=(UI_FONT, 9), anchor="w")
        self.progress_label.pack(fill="x")

        row2 = tk.Frame(inner2, bg=C_CARD)
        row2.pack(fill="x", pady=(10, 0))
        self.btn_open = ttk.Button(row2, text="看今天的日记",
                                   command=self.on_open_today)
        self.btn_open.pack(side="left", expand=True, fill="x", padx=(0, 6))
        self.btn_week = ttk.Button(row2, text="本周周报",
                                   command=self.on_weekly)
        self.btn_week.pack(side="left", expand=True, fill="x", padx=(6, 0))

        # ---------- 计划卡片 ----------
        card3 = tk.Frame(outer, bg=C_CARD, highlightbackground="#e4e8f0",
                         highlightthickness=1)
        card3.pack(fill="both", expand=True, pady=(0, 10))
        inner3 = tk.Frame(card3, bg=C_CARD)
        inner3.pack(fill="both", expand=True, padx=14, pady=12)

        tk.Label(inner3, text="今日计划", bg=C_CARD, fg=C_TEXT,
                 font=(UI_FONT, 12, "bold")).pack(anchor="w")
        tk.Label(inner3, text="做完一项就打个勾。",
                 bg=C_CARD, fg=C_MUTED, font=(UI_FONT, 9),
                 anchor="w").pack(fill="x", pady=(2, 6))

        self.plan_frame = tk.Frame(inner3, bg=C_CARD)
        self.plan_frame.pack(fill="both", expand=True)
        self.refresh_plan()

        # ---------- 日志 ----------
        self.log_box = tk.Text(outer, height=6, bg="#1a202c", fg="#cbd5e0",
                               font=("Consolas", 9), relief="flat",
                               insertbackground="#cbd5e0")
        self.log_box.pack(fill="both", expand=False)
        self.log_box.configure(state="disabled")

    # ============================================================
    #  计划区
    # ============================================================
    def refresh_plan(self):
        for w in self.plan_frame.winfo_children():
            w.destroy()
        self.plan_done_vars.clear()

        cfg = config.load_config()
        plan = cfg.get("plan", [])
        done_map = cfg.get("plan_done", {})

        if not plan:
            tk.Label(self.plan_frame, text="还没设计划。右上角「设置 ▾」里可以设。",
                     bg=C_CARD, fg=C_MUTED, font=(UI_FONT, 10)).pack(anchor="w")
            return

        for item in plan:
            task = (item.get("task") or "").strip()
            if not task:
                continue
            row = tk.Frame(self.plan_frame, bg=C_CARD)
            row.pack(fill="x", pady=2)
            var = tk.BooleanVar(value=bool(done_map.get(task)))
            self.plan_done_vars[task] = var
            cb = tk.Checkbutton(
                row, variable=var, bg=C_CARD, activebackground=C_CARD,
                command=lambda t=task: self.on_toggle_done(t),
                highlightthickness=0, bd=0)
            cb.pack(side="left")
            tk.Label(row, text=f"{item.get('start','')}-{item.get('end','')}",
                     bg=C_CARD, fg=C_MUTED, font=(UI_FONT, 9),
                     width=12, anchor="w").pack(side="left")
            tk.Label(row, text=task, bg=C_CARD, fg=C_TEXT,
                     font=(UI_FONT, 10), anchor="w").pack(side="left")

    def on_toggle_done(self, task: str):
        cfg = config.load_config()
        done = cfg.get("plan_done", {})
        done[task] = bool(self.plan_done_vars[task].get())
        cfg["plan_done"] = done
        config.save_config(cfg)
        self.log(f"[计划] {task} -> {'完成' if done[task] else '未完成'}")

    def on_set_plan(self):
        dlg = PlanDialog(self.root, config.load_config().get("plan", []))
        self.root.wait_window(dlg.top)
        if dlg.result is not None:
            cfg = config.load_config()
            old = cfg.get("plan", [])
            cfg["plan"] = dlg.result
            # 计划里删掉的任务，打勾记录也一并清掉
            keep = {i["task"] for i in dlg.result if i.get("task")}
            cfg["plan_done"] = {k: v for k, v in cfg.get("plan_done", {}).items()
                                if k in keep}
            config.save_config(cfg)
            self.refresh_plan()
            self.log(f"[计划] 已更新，共 {len(dlg.result)} 项")

    # ============================================================
    #  手动记录
    # ============================================================
    def on_start(self):
        task = simpledialog.askstring(
            "开始记录", "这一段打算干什么？\n（可以留空）",
            parent=self.root)
        if task is None:
            return
        s = self.store.start_session(task or "")
        self.log(f"[记录] 开始 #{s['id']} {s['task'] or '（未命名）'}")
        self.update_session_ui()

    def on_end(self):
        s = self.store.end_session()
        if not s:
            self.log("[记录] 没有进行中的记录")
            return
        self.update_session_ui()

        # 立刻给这一段算个分配
        events = self.store.read_events()
        start_ts = float(s.get("start_ts") or 0)
        end_ts = float(s.get("end_ts") or 0)
        seg = [e for e in events
               if e.get("kind") == "window" and start_ts <= float(e.get("ts") or 0) <= end_ts]
        stats = compute(seg)

        lines = [f"这一段：{fmt_duration(s.get('seconds', 0))}"]
        if s.get("task"):
            lines.append(f"任务：{s['task']}")
        if stats["apps"]:
            lines.append("")
            for a in stats["apps"][:6]:
                lines.append(f"  {a['name']}  {fmt_duration(a['seconds'])}  "
                             f"{a['share']*100:.0f}%")
        else:
            lines.append("（这段时间没有窗口切换记录）")
        self.log("[记录] " + " / ".join(
            [f"结束 #{s['id']}"] + [f"{a['name']} {fmt_duration(a['seconds'])}"
                                    for a in stats["apps"][:4]]))
        messagebox.showinfo("这一段的结果", "\n".join(lines), parent=self.root)

    def update_session_ui(self):
        s = self.store.open_session()
        if s:
            self.btn_start.configure(state="disabled")
            self.btn_end.configure(state="normal")
            self.session_label.configure(
                text=f"进行中：{s['task'] or '（未命名）'}\n从 {s['start'][-8:]} 开始",
                fg=C_GREEN)
        else:
            self.btn_start.configure(state="normal")
            self.btn_end.configure(state="disabled")
            sessions = self.store.read_sessions()
            done = [x for x in sessions if x.get("end")]
            if done:
                total = sum(x.get("seconds", 0) for x in done)
                self.session_label.configure(
                    text=f"没有进行中的记录（今天已完成 {len(done)} 段，"
                         f"合计 {fmt_duration(total)}）", fg=C_MUTED)
            else:
                self.session_label.configure(text="没有进行中的记录", fg=C_MUTED)

    # ============================================================
    #  分析
    # ============================================================
    def on_startup_tasks(self):
        """
        启动时的四件杂事：
          0. 恢复上次的专注模式状态
          1. 把旧结构的日记迁到「月/周/天」三级目录
          2. 清理过期截图
          3. 提醒有记录但没分析的日子

        为什么放在这里而不是后台静默做：程序是常驻的，你可能忘了点分析
        就关机了。不提醒的话那天的记录就一直躺在 data/ 里 —— 你看不见、
        分析不了、截图还占着磁盘。
        """
        cfg = config.load_config()

        # 0) 专注模式：上次开着就继续开着
        try:
            self.apply_focus_from_config(cfg)
            # 自动关闭要回调界面，把勾选框一起取消
            self.collector.on_focus_change = self.on_focus_changed
        except Exception as e:
            self.log(f"[专注] 恢复失败：{e}")

        # 0.5) data 目录按年月整理 + 上月归档（每月 7 号之后）
        try:
            Store.migrate_data_layout(logger=self.log)
        except Exception as e:
            self.log(f"[搬迁] 失败：{e}")
        try:
            done = Store.archive_due_months(logger=self.log)
            if done:
                self.log(f"[归档] 共归档 {len(done)} 个月：{'、'.join(done)}")
        except Exception as e:
            self.log(f"[归档] 失败：{e}")

        # 0) 目录结构迁移
        #
        # 这个必须实际调用，否则旧日记会永远留在老结构里 —— 迁移逻辑写了
        # 但没人调，等于没做（实测踩过：功能都在，就是不生效）。
        try:
            r = self.store.migrate_diary_layout(logger=self.log)
            if r["moved"]:
                self.log(f"[迁移] 日记已按新结构整理："
                         f"{r['moved']} 篇正文、{r['pics']} 张图")
            if r["conflicts"]:
                self.log(f"[迁移] {len(r['conflicts'])} 篇同名旧日记"
                         f"存成了 .old.md，没有覆盖")
        except Exception as e:
            self.log(f"[迁移] 失败：{e}")

        # 1) 清理过期截图（只留最近 N 天，给「忘了分析」留补分析的机会）
        try:
            import screenshots
            keep = int(cfg.get("keep_shots_days", config.KEEP_SHOTS_DAYS))
            n = screenshots.cleanup_old_shots(config.SHOT_DIR, keep_days=keep,
                                              logger=self.log)
            if n:
                self.log(f"[清理] 删除了 {n} 个超过 {keep} 天的截图目录")
        except Exception as e:
            self.log(f"[清理] 失败：{e}")

        # 2) 有记录但没日记的日子
        #
        # 这里只负责「发现并提醒」，不自己决定分析哪天 —— 交给补分析窗口，
        # 你可以勾选要补的日子。以前是个 yes/no，多个未分析的日子只能选
        # 「最近一天」，缺 4 天就得开 4 次程序。
        try:
            pending = Store.pending_days(before=self.day)
        except Exception as e:
            self.log(f"[检查] 失败：{e}")
            pending = []

        if not pending:
            return

        old = Store.orphans_older_than(7)
        self.log(f"[检查] 发现 {len(pending)} 天未分析：{', '.join(pending)}")
        if old:
            self.log(f"[提示] 另有 {len(old)} 天超过 7 天未分析，"
                     f"截图已被清理，之后只能靠窗口标题统计")

        if messagebox.askyesno(
                "有记录没分析",
                f"有 {len(pending)} 天记了但还没生成日记：\n"
                f"{'、'.join(pending[-6:])}"
                f"{'（共 %d 天）' % len(pending) if len(pending) > 6 else ''}\n\n"
                f"现在挑要补的日子吗？",
                parent=self.root):
            self.on_batch_analyze()

    def analyze_day(self, day: str):
        """分析指定某一天（补分析走这里）。"""
        if self.busy:
            messagebox.showinfo("稍等", "正在忙，先等这次跑完。", parent=self.root)
            return
        self.analyzing_day = day
        self.busy = True
        self.stop_requested = False
        self.btn_analyze.configure(text="分析中…（再按一次可中止）",
                                   command=self.on_cancel_analyze)
        self.log(f"[分析] 开始处理 {day}")
        threading.Thread(target=self._analyze_worker, daemon=True,
                         name="analyze").start()

    def on_analyze(self):
        if self.busy:
            messagebox.showinfo("稍等", "正在忙，先等这次跑完。", parent=self.root)
            return

        shot_dir = self.store.shot_dir()
        n = len(list(shot_dir.glob("*.jpg")))
        if n == 0:
            if not messagebox.askyesno(
                    "没有截图",
                    "今天没有截图，分析出来的日记会只有软件时长。\n要继续吗？",
                    parent=self.root):
                return
        else:
            est = n * config.VISION_IMAGE_SECONDS / 60
            if not messagebox.askyesno(
                    "开始分析",
                    f"共 {n} 张截图。\n"
                    f"预计需要约 {est:.0f} 分钟（实测每张约 1.2 秒）。\n"
                    f"分析期间会比较吃显卡，建议你现在真的不用电脑。\n\n"
                    f"开始吗？",
                    parent=self.root):
                return

        # 分析前先把进行中的手动记录收尾，免得时长算一半
        if self.store.open_session():
            self.store.end_session()
            self.update_session_ui()

        self.analyzing_day = self.day
        self.busy = True
        self.stop_requested = False
        self.btn_analyze.configure(text="分析中…（再按一次可中止）",
                                   command=self.on_cancel_analyze)
        self.log("[分析] 开始")
        threading.Thread(target=self._analyze_worker, daemon=True,
                         name="analyze").start()

    def on_cancel_analyze(self):
        if self.busy:
            self.stop_requested = True
            self.log("[分析] 收到中止请求，正在收尾…")

    def _analyze_worker(self):
        try:
            import diary
            day = getattr(self, "analyzing_day", None) or self.day
            result = diary.run_full_analysis(
                day,
                progress=lambda stage, d, t, m: self.msg_q.put(("prog", stage, d, t, m)),
                should_stop=lambda: self.stop_requested,
                logger=self.log,
                delete_shots_after=not self.stop_requested,
            )
            self.msg_q.put(("done", result))
        except Exception as e:
            import traceback
            self.msg_q.put(("error", str(e), traceback.format_exc()))

    # ============================================================
    #  打开日记
    # ============================================================
    # ============================================================
    #  专注模式
    # ============================================================
    def on_toggle_focus(self):
        """
        开关专注模式。

        开着时「离开」阈值从 10 分钟拉长到 2 小时，那段时间一律算活跃。
        解决的是这个场景：看网课 / 思考难题时你几十分钟不碰键鼠，
        但人就在电脑前 —— 默认阈值会把这段时间整段判成离开，
        实测「看 40 分钟网课」只记到 1 分钟。

        代价是这段时间里你真走了也会算在用，所以它必须是个手动开关，
        而且要一眼看得见状态（标题下面那行）。**两小时后会自动关掉**，
        免得你忘了关、后面真走开的时间全被算成活跃。
        """
        on = bool(self.focus_var.get())
        try:
            self.collector.set_focus_mode(on)
        except Exception as e:
            self.log(f"[专注] 切换失败：{e}")

        cfg = config.load_config()
        cfg["focus_mode"] = on
        config.save_config(cfg)

        if on:
            self.focus_check.configure(fg=C_BLUE)
            self.log(f"[专注] 已开启，离开阈值拉长到 "
                     f"{config.FOCUS_IDLE_SECONDS // 3600} 小时，"
                     f"{config.FOCUS_AUTO_OFF_SECONDS // 3600} 小时后自动关闭")
        else:
            self.focus_check.configure(fg=C_TEXT)
            self.focus_hint.configure(text="")
            self.log(f"[专注] 已关闭，离开阈值回到 "
                     f"{config.AWAY_IDLE_SECONDS // 60} 分钟")
        self._refresh_focus_hint()

    def _refresh_focus_hint(self):
        """刷新专注模式的提示文字（含自动关闭倒计时）。"""
        try:
            if not self.focus_var.get():
                self.focus_hint.configure(text="")
                return
            left = 0
            try:
                left = self.collector.focus_seconds_left()
            except Exception:
                pass
            if left:
                h, m = divmod(left // 60, 60)
                tail = f"，{h} 小时 {m} 分后自动关" if h else f"，{m} 分后自动关"
            else:
                tail = ""
            self.focus_hint.configure(
                text=f"已开启 · 离开阈值 "
                     f"{config.FOCUS_IDLE_SECONDS // 3600} 小时，"
                     f"这段时间一律算活跃{tail}")
        except Exception:
            pass

    def on_focus_changed(self, on: bool, reason: str = ""):
        """采集器改了专注状态时回调（目前只有「到点自动关」会走这里）。"""
        try:
            self.focus_var.set(bool(on))
            if not on:
                self.focus_check.configure(fg=C_TEXT)
                self.focus_hint.configure(text="")
                cfg = config.load_config()
                cfg["focus_mode"] = False
                config.save_config(cfg)
                self._write_log("[专注] 已满 2 小时，自动关闭")
        except Exception:
            pass

    def apply_focus_from_config(self, cfg: dict):
        """启动时把上次的专注状态恢复回来。"""
        on = bool(cfg.get("focus_mode"))
        self.focus_var.set(on)
        try:
            self.collector.set_focus_mode(on)
        except Exception as e:
            self.log(f"[专注] 恢复失败：{e}")
        if on:
            self.focus_check.configure(fg=C_BLUE)
            self.log("[专注] 已恢复上次的状态：开启")
        self._refresh_focus_hint()

    # ============================================================
    #  设置菜单
    # ============================================================
    def on_settings_menu(self):
        """
        弹出设置菜单，把「偶尔用一次」的功能都收在这里。

        用 tk.Menu(tearoff=0) 而不是新窗口：这些项都是「点一下就做完」的，
        再开一个窗口反而多一层。菜单贴着按钮右下角弹出来。
        """
        m = tk.Menu(self.root, tearoff=0, font=(UI_FONT, 10))

        m.add_command(label="浏览日记 / 周报…", command=self.on_open_diary)
        m.add_command(label="本周周报…", command=self.on_weekly)
        m.add_separator()
        m.add_command(label="设置今天的计划…", command=self.on_set_plan)
        m.add_command(label="补分析没生成的日子…", command=self.on_batch_analyze)
        m.add_separator()
        m.add_command(label="打开日记文件夹", command=self.on_open_diary_dir)
        m.add_command(label="打开数据文件夹", command=self.on_open_data_dir)

        # 贴着按钮左下角弹出（tk 的 post 用屏幕坐标）
        try:
            x = self.btn_settings.winfo_rootx()
            y = self.btn_settings.winfo_rooty() + self.btn_settings.winfo_height()
            m.tk_popup(x, y)
        finally:
            m.grab_release()

    def on_open_diary_dir(self):
        try:
            os.startfile(str(config.DIARY_DIR))
        except Exception as e:
            self.log(f"[打开] 失败：{e}")

    def on_open_data_dir(self):
        try:
            os.startfile(str(config.DATA_DIR))
        except Exception as e:
            self.log(f"[打开] 失败：{e}")

    # ============================================================
    #  打开日记
    # ============================================================
    def on_batch_analyze(self):
        """
        打开补分析窗口。列出「有记录的日子」，包含已分析的和没分析的。

        为什么要连已分析的一起列：你可能想拿旧记录重新生成日记
        （换了计划、模型更新、或者上次分析中途中止了）。
        """
        if self.busy:
            messagebox.showinfo("稍等", "正在忙，先等这次跑完。", parent=self.root)
            return

        days = self._collect_candidates()
        if not days:
            messagebox.showinfo(
                "没有可分析的日子",
                "data/ 里还没有任何窗口记录。\n"
                "先让它记一会儿，再来补分析。", parent=self.root)
            return

        BatchAnalyzeDialog(self.root, days, self.start_batch)

    def _collect_candidates(self) -> list[dict]:
        """
        列出所有可以分析的日子：有窗口事件的每一天。

        事件条数和截图张数都先数出来给对话框显示 —— 用户靠这个判断
        「那天值不值得补」。截图数要扫目录，所以只扫存在的。
        """
        out: list[dict] = []
        for day in Store.days_with_data():
            store = Store(day)
            try:
                events = store.read_events(day)
            except Exception:
                continue
            wins = sum(1 for e in events if e.get("kind") == "window")
            if not wins:
                continue          # 只有 rollover 之类的空日子，没有分析价值
            try:
                shots = len(list(store.shot_dir().glob("*.jpg")))
            except Exception:
                shots = 0
            done = store.diary_path(day).exists()
            try:
                wd = "一二三四五六日"[date.fromisoformat(day).weekday()]
            except Exception:
                wd = ""
            out.append({"day": day, "wd": wd, "events": wins, "shots": shots,
                        "done": done, "note": ""})
        return out

    def start_batch(self, days: list[dict]):
        """批量分析：逐天跑，中途可中止，单天失败跳过。"""
        if self.busy:
            messagebox.showinfo("稍等", "正在忙，先等这次跑完。", parent=self.root)
            return

        # 分析前先把进行中的手动记录收尾，免得时长算一半
        if self.store.open_session():
            self.store.end_session()
            self.update_session_ui()

        self.batch_days = [d["day"] for d in days]
        self.batch_done: list[str] = []
        self.batch_failed: list[tuple[str, str]] = []
        self.busy = True
        self.stop_requested = False
        self.btn_analyze.configure(text="批量分析中…（再按一次可中止）",
                                   command=self.on_cancel_analyze)
        self.log(f"[补分析] 共 {len(self.batch_days)} 天，开始")
        threading.Thread(target=self._batch_worker, daemon=True,
                         name="batch").start()

    def _batch_worker(self):
        import diary
        total = len(self.batch_days)
        try:
            for i, day in enumerate(self.batch_days, 1):
                if self.stop_requested:
                    self.log(f"[补分析] 已中止，剩余 {total - i + 1} 天跳过")
                    break
                self.msg_q.put(("prog", f"补分析 {i}/{total}", 0, 0,
                                f"正在处理 {day}"))
                self.msg_q.put(("log", f"[补分析] ({i}/{total}) {day}"))
                try:
                    # 重跑已有日记时先留一份备份（旧内容可能被手工改过）
                    self._backup_existing(day)
                    diary.run_full_analysis(
                        day,
                        progress=lambda s, d, t, m: self.msg_q.put(
                            ("prog", f"{day} {s}", d, t, m)),
                        should_stop=lambda: self.stop_requested,
                        logger=self.log,
                        delete_shots_after=not self.stop_requested,
                        # 批量时保持加载，整批跑完再卸一次
                        keep_model_loaded=True,
                    )
                    self.batch_done.append(day)
                    self.msg_q.put(("log", f"[补分析] {day} 完成"))
                except Exception as e:
                    self.batch_failed.append((day, str(e)))
                    self.msg_q.put(("log", f"[补分析] {day} 失败：{e}"))
                    # 单天失败不影响后面的日子
                    continue
        finally:
            # 整批跑完（或中止）后统一释放显存
            try:
                import vision as _v
                _v.unload(config.VISION_MODEL)
                if config.TEXT_MODEL != config.VISION_MODEL:
                    _v.unload(config.TEXT_MODEL)
                self.msg_q.put(("log", "[补分析] 已释放显存"))
            except Exception:
                pass
            self.msg_q.put(("batch_done", None))

    def _backup_existing(self, day: str):
        """
        重跑前把旧日记另存成 diary.old.md。

        为什么：diary.md 可能被你手工改过（在总结后面补几句自己的话），
        重跑会直接覆盖。留一份就还能捞回来。
        只保留最近一次备份，所以固定用同一个文件名，不会越堆越多。
        """
        p = self.store.diary_path(day)
        if not p.exists():
            return
        try:
            alt = p.with_name("diary.old.md")
            if alt.exists():
                alt.unlink()
            p.replace(alt)
            self.msg_q.put(("log", f"[补分析] 旧日记已备份为 {alt.name}"))
        except Exception as e:
            self.msg_q.put(("log", f"[补分析] 备份失败（继续）：{e}"))

    def _on_batch_done(self):
        self.busy = False
        self.btn_analyze.configure(text="我有空，开始分析",
                                   command=self.on_analyze)
        ok, bad = len(getattr(self, "batch_done", [])), \
            len(getattr(self, "batch_failed", []))
        self.progress.configure(value=100 if not bad else 0)
        self.progress_label.configure(
            text=f"补分析完成 · 成功 {ok} 天" + (f"，失败 {bad} 天" if bad else ""))
        self.log(f"[补分析] 结束：成功 {ok} 天，失败 {bad} 天")

        # 涉及的那几周如果已有周报，问一句要不要重算
        weeks = set()
        for d in getattr(self, "batch_done", []):
            try:
                weeks.add(self.store.week_key(d))
            except Exception:
                pass
        stale = []
        for wk in sorted(weeks):
            try:
                p = self.store.weekly_path(wk, self.store._week_monday(wk))
                if p.exists():
                    stale.append(wk)
            except Exception:
                pass
        if stale:
            from tkinter import messagebox as mb
            if mb.askyesno(
                    "周报要重算吗",
                    f"这些天的数据变了：{'、'.join(stale)}\n"
                    f"对应的周报是基于旧数据生成的。\n\n"
                    f"要现在重算吗？（会覆盖现有周报）",
                    parent=self.root):
                self.recompute_weeks(stale)

    def recompute_weeks(self, weeks: list[str]):
        """重算若干周的周报（后台跑，逐个来）。"""
        if self.busy:
            return
        self.busy = True
        self.stop_requested = False
        self.btn_analyze.configure(text="重算周报中…")
        self.log(f"[周报] 重算 {len(weeks)} 份")

        def work():
            try:
                import weekly as W
                for wk in weeks:
                    self.msg_q.put(("log", f"[周报] 重算 {wk}"))
                    W.run_weekly(wk, logger=self.log)
                self.msg_q.put(("log", "[周报] 重算完成"))
            except Exception as e:
                self.msg_q.put(("log", f"[周报] 重算失败：{e}"))
            finally:
                self.msg_q.put(("weekly_done", None))

        threading.Thread(target=work, daemon=True, name="weekly").start()

    def on_open_diary(self):
        """
        打开日记浏览窗口。

        以前这个按钮是「看今天的日记」，没有今天的就打开最近一篇 ——
        有了周报之后那样不够用：你可能想翻上周的、或者直接看周报。
        现在统一进浏览窗口，进去就定位到今天（如果有）。
        """
        if getattr(self, "_browser", None) is not None:
            try:
                if self._browser.winfo_exists():
                    self._browser.refresh()
                    self._browser.lift()
                    self._browser.focus_force()
                    return
            except Exception:
                pass
        self._browser = DiaryBrowser(self.root, self.store, self.day)

    def on_open_today(self):
        """直接打开今天那篇（不看浏览窗口，少点两下）。"""
        p = self.store.diary_path(self.day)
        if not p.exists():
            all_d = self.store.list_diaries()
            if not all_d:
                messagebox.showinfo("还没有日记",
                                    "今天还没生成日记。\n"
                                    "先按「我有空，开始分析」。", parent=self.root)
                return
            p = all_d[0]
        try:
            os.startfile(str(p))
        except Exception:
            subprocess.Popen(["notepad.exe", str(p)])

    def on_weekly(self):
        """
        手动生成 / 重算本周周报。

        为什么要有这个按钮：周报默认只在**周日**分析完当天日记时自动生成。
        但你会漏 —— 周日忘了分析、或者周四才补上周三的日记，那一周的周报
        就一直是旧的（或者根本没有）。没有这个入口就只能改系统时间去骗它。
        """
        import weekly as W
        week = W.week_key(self.day)
        agg = W.aggregate_week(week)
        if not agg.get("days_with_data"):
            messagebox.showinfo(
                "这周还没有记录",
                f"{week}（{W.week_range_label(week)}）还没有任何记录，\n"
                f"先把每天分析出来再生成周报。", parent=self.root)
            return

        p = self.store.weekly_path(week, self.day)
        exists = p.exists()
        msg = (f"生成 {week} 的周报？\n\n"
               f"这一周有记录：{agg['days_with_data']}/7 天\n"
               f"活跃合计：{agg['total_active_seconds'] // 3600} 小时 "
               f"{agg['total_active_seconds'] % 3600 // 60} 分\n\n")
        msg += ("已经有一份了，会被覆盖重算。" if exists
                else "生成后会放到：\n" + str(p.parent))
        if not messagebox.askyesno("生成周报", msg, parent=self.root):
            return

        if self.busy:
            messagebox.showinfo("稍等", "正在忙，先等这次跑完。", parent=self.root)
            return

        self.busy = True
        self.stop_requested = False
        self.btn_analyze.configure(text="正在生成周报…")
        self.log(f"[周报] 开始生成 {week}")

        def work():
            try:
                r = W.run_weekly(week, logger=self.log)
                self.msg_q.put(("log", f"[周报] 已写入 {r['diary']}"))
                self.msg_q.put(("open", r["diary"]))
            except Exception as e:
                self.msg_q.put(("log", f"[周报] 失败：{e}"))
            finally:
                self.msg_q.put(("weekly_done", None))

        threading.Thread(target=work, daemon=True, name="weekly").start()

    def _on_weekly_done(self):
        self.busy = False
        self.btn_analyze.configure(text="我有空，开始分析",
                                   command=self.on_analyze)

    # ============================================================
    #  日志与主循环
    # ============================================================
    def log(self, msg: str):
        self.msg_q.put(("log", msg))

    def _write_log(self, msg: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", time.strftime("[%H:%M:%S] ") + msg + "\n")
        # 只留最后 400 行，防内存涨
        if int(self.log_box.index("end-1c").split(".")[0]) > 400:
            self.log_box.delete("1.0", "100.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _drain_queue(self):
        try:
            while True:
                item = self.msg_q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._write_log(item[1])
                elif kind == "prog":
                    _, stage, d, t, m = item
                    self._on_progress(stage, d, t, m)
                elif kind == "done":
                    self._on_analyze_done(item[1])
                elif kind == "error":
                    self._on_analyze_error(item[1], item[2])
                elif kind == "weekly_done":
                    self._on_weekly_done()
                elif kind == "batch_done":
                    self._on_batch_done()
                elif kind == "open":
                    # 生成完直接打开成品，省得你自己去翻目录
                    try:
                        os.startfile(str(item[1]))
                    except Exception:
                        pass
        except queue.Empty:
            pass

        # 状态刷新
        st = self.collector.status()
        if st["running"]:
            self.status_dot.configure(fg=C_GREEN)
            idle_note = ""
            if st["idle"] >= 300:
                idle_note = f"，闲置 {st['idle']//60} 分"
            self.status_text.configure(text="记录中", fg=C_GREEN)
            self.status_detail.configure(
                text=f"已记录 {fmt_duration(st['seconds'])}   "
                     f"{st['events']} 次切换   {st['shots']} 张截图{idle_note}\n"
                     f"当前：{st['proc'] or '—'}   {st['title'][:52]}")
        else:
            self.status_dot.configure(fg=C_MUTED)
            self.status_text.configure(text="未开始", fg=C_MUTED)
            self.status_detail.configure(
                text="采集没有运行。点下面的「开始记录」启动手动记录，\n"
                     "或直接开始分析今天已有的记录。")

        self.root.after(700, self._drain_queue)

    def _on_progress(self, stage, d, t, m):
        if t:
            pct = int(d / t * 100)
            self.progress.configure(value=pct)
            self.progress_label.configure(
                text=f"{stage}  {d}/{t}   {m[:56]}")
        else:
            self.progress.configure(value=0 if stage != "完成" else 100)
            self.progress_label.configure(text=f"{stage}   {m[:56]}")

    def _on_analyze_done(self, result):
        self.busy = False
        self.btn_analyze.configure(text="我有空，开始分析",
                                   command=self.on_analyze)
        self.progress.configure(value=100)
        self.last_result = result
        st = result.get("stats", {})
        self.progress_label.configure(
            text=f"完成 · 活跃 {fmt_duration(st.get('total_active_seconds', 0))}")
        self._write_log(f"[分析] 完成，日记：{result['diary']}")

        if messagebox.askyesno("日记写好了",
                               f"日记：{Path(result['diary']).name}\n\n"
                               f"现在打开看吗？",
                               parent=self.root):
            self.on_open_diary()

    def _on_analyze_error(self, err, tb):
        self.busy = False
        self.btn_analyze.configure(text="我有空，开始分析",
                                   command=self.on_analyze)
        self.progress.configure(value=0)
        self._write_log(f"[分析] 出错：{err}")
        self._write_log(tb.splitlines()[-1] if tb else "")
        messagebox.showerror("分析失败", err, parent=self.root)

    # ============================================================
    #  定时刷新
    # ============================================================
    def _tick(self):
        self.update_session_ui()
        # 专注模式的倒计时（每 2 秒刷一次够了，只有一句话要更新）
        if getattr(self, "focus_var", None) is not None and self.focus_var.get():
            self._refresh_focus_hint()
        self.root.after(2000, self._tick)

    def on_close(self):
        if self.collector.running():
            if not messagebox.askyesno("退出", "还在记录中，确定退出吗？",
                                       parent=self.root):
                return
        s = self.store.open_session()
        if s:
            self.store.end_session()
        self.collector.stop()
        self.root.destroy()


class PlanDialog:
    """设置今日计划的弹窗。每行「开始-结束 任务」。"""

    def __init__(self, parent, plan: list[dict]):
        self.result = None
        top = tk.Toplevel(parent)
        self.top = top
        top.title("设置今日计划")
        top.configure(bg=C_BG)
        top.geometry("460x420")
        top.transient(parent)
        top.grab_set()

        tk.Label(top, text="每行一个计划，格式：开始-结束 任务",
                 bg=C_BG, fg=C_TEXT, font=(UI_FONT, 11, "bold")).pack(
            anchor="w", padx=14, pady=(12, 2))
        tk.Label(top, text="例如：09:00-11:00 写代码",
                 bg=C_BG, fg=C_MUTED, font=(UI_FONT, 9)).pack(anchor="w", padx=14)

        self.text = tk.Text(top, height=13, font=("Consolas", 11),
                            relief="flat", bg="#ffffff", fg=C_TEXT)
        self.text.pack(fill="both", expand=True, padx=14, pady=10)

        lines = []
        for it in plan or []:
            if it.get("task"):
                lines.append(f"{it.get('start','')}-{it.get('end','')} {it['task']}")
        self.text.insert("1.0", "\n".join(lines))

        row = tk.Frame(top, bg=C_BG)
        row.pack(fill="x", padx=14, pady=(0, 14))
        ttk.Button(row, text="取消", command=self._cancel).pack(
            side="right", padx=(6, 0))
        ttk.Button(row, text="保存", command=self._save).pack(side="right")

    def _cancel(self):
        self.result = None
        self.top.destroy()

    def _save(self):
        raw = self.text.get("1.0", "end").strip()
        out = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            rng = parts[0]
            task = parts[1].strip() if len(parts) > 1 else ""
            if "-" in rng:
                a, b = rng.split("-", 1)
                out.append({"start": a.strip(), "end": b.strip(), "task": task})
            elif task:
                out.append({"start": "", "end": "", "task": task})
        self.result = out
        self.top.destroy()


def main():
    enable_dpi_awareness()
    config.ensure_dirs()
    root = tk.Tk()
    app = JournalApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)

    # 自动开始采集：默认模式就是「一直记着，你不用管」
    root.after(600, lambda: app.collector.start())
    # 清理过期截图 + 提醒没分析的日子（等界面画完再做）
    root.after(1200, app.on_startup_tasks)

    root.mainloop()


if __name__ == "__main__":
    main()
