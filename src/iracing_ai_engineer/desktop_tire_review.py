"""Native, explicit post-session review; no preselected service assertions."""

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from .tire_review import SKIP

CHOICES = {"整套四条新胎": "FULL_NEW_SET", "没有换胎": "NO_TIRE_CHANGE",
           "部分更换／不确定": "PARTIAL_OR_UNKNOWN", "未审核（不建立标签）": SKIP}
DISPLAY = {value: key for key, value in CHOICES.items()}


class TireReviewWindow:
    def __init__(self, parent, controller, plan):
        self.controller, self.plan = controller, plan
        self.window = tk.Toplevel(parent)
        self.window.title("AEIS · 赛后换胎证据审核")
        self.window.geometry("850x680")
        self.window.minsize(780, 650)
        self.window.transient(parent)
        self.current = None
        self.evidence_path = None
        self.decisions = [{"ordinal": row["ordinal"], "kind": "", "evidence_path": None,
                           "evidence_offset_s": None} for row in plan["exits"]]
        panel = ttk.Frame(self.window, padding=16)
        panel.pack(fill="both", expand=True)
        if plan.get("synthetic_demo") is True:
            ttk.Label(panel, text="合成界面演示 · 非真实记录 · 此模式不能导出").pack(anchor="w")
        ttk.Label(panel, text="逐次审核实际换胎；车内确认仅供对照，不自动采纳。",
                  wraplength=790).pack(anchor="w")
        ttk.Label(panel, text="为每次出站选择一种处理。需引用保留在本机的服务记录或回放证据；"
                  "原始采集／确认日志本身不能证明换胎。\n"
                  "选择“未审核”不会生成该次标签，旧胎龄可能因此失效。", wraplength=790,
                  style="Muted.TLabel").pack(anchor="w", pady=8)
        ttk.Label(panel, text=f"可定位出站：{len(plan['exits'])} 次；"
                  f"胎组字段／连续性不可用：{plan.get('unavailable_frames', 0)} 帧。"
                  "不会补猜遗漏的出站。", wraplength=790).pack(anchor="w", pady=(0, 8))
        table = ttk.Frame(panel)
        table.pack(fill="both", expand=True)
        style = ttk.Style(self.window)
        style.configure("TireReview.Treeview", background="#152130", fieldbackground="#152130",
                        foreground="#edf3f8", rowheight=26)
        style.map("TireReview.Treeview", background=[("selected", "#315769")],
                  foreground=[("selected", "#ffffff")])
        self.tree = ttk.Treeview(table,
                                columns=("exit", "lap", "compound", "sets", "hint", "choice"),
                                show="headings", height=5, selectmode="browse",
                                style="TireReview.Treeview")
        for key, label, width in (("exit", "出站 Tick", 90), ("lap", "完成圈", 70),
                                  ("compound", "胎种", 60), ("sets", "套数计数", 70),
                                  ("hint", "车内自述（非审核结果）", 230),
                                  ("choice", "本次审核选择", 230)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=70)
        bar = ttk.Scrollbar(table, command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        for row in plan["exits"]:
            hint = DISPLAY.get(row["driver_assertion"], "没有对应车内确认")
            if row["driver_assertion"] and not row["assertion_recomputed"]:
                hint += "（重算未接受）"
            self.tree.insert("", "end", iid=str(row["ordinal"]), values=(
                row["decision_tick"], row["laps_completed"], row.get("tire_compound", "—"),
                row.get("tire_sets_used", "—"), hint, "尚未选择"))
        self.kind = tk.StringVar(self.window)
        self.offset = tk.StringVar(self.window)
        self.evidence = tk.StringVar(self.window, "尚未选择证据")
        self.notice = tk.StringVar(self.window)
        self.attested = tk.BooleanVar(self.window, False)
        self.kind_box = ttk.Combobox(panel, textvariable=self.kind, state="readonly",
                                    values=list(CHOICES), width=36)
        self.kind_box.pack(anchor="w", pady=(10, 6))
        evidence_row = ttk.Frame(panel)
        evidence_row.pack(fill="x")
        ttk.Button(evidence_row, text="选择本机证据…", command=self._choose_evidence).pack(
            side="left")
        ttk.Label(evidence_row, textvariable=self.evidence, wraplength=570).pack(
            side="left", padx=10)
        offset_row = ttk.Frame(panel)
        offset_row.pack(fill="x", pady=8)
        ttk.Label(offset_row, text="证据视频位置（整数秒，可留空）：").pack(side="left")
        ttk.Entry(offset_row, textvariable=self.offset, width=12).pack(side="left")
        ttk.Button(offset_row, text="保存这一项", command=self._save).pack(side="left", padx=12)
        ttk.Checkbutton(panel, variable=self.attested,
            text="我已逐项核对实际服务；未把计数、进站请求或车内按钮当作换胎证明。").pack(
                anchor="w", pady=8)
        ttk.Label(panel, text="导出是人工自述记录，不认证人员／服务真伪，不等于磨损或比赛验收。"
                  "证据文件不上传、不播放；请自行保留原件。", wraplength=790,
                  style="Muted.TLabel").pack(anchor="w")
        ttk.Label(panel, textvariable=self.notice, wraplength=790).pack(anchor="w", pady=6)
        self.export_button = ttk.Button(panel, text="导出至本机私有目录", command=self._submit)
        self.export_button.pack(anchor="e")
        self.tree.bind("<<TreeviewSelect>>", self._select)
        if plan["exits"]:
            self.tree.selection_set("1")

    def _save(self):
        if self.current is None:
            return
        kind = CHOICES.get(self.kind.get(), "")
        value = self.offset.get().strip()
        if value and (not value.isascii() or not value.isdecimal() or len(value) > 6):
            self.notice.set("视频位置必须是 0 到 604800 的整数秒，或留空。")
            return False
        offset = int(value) if value else None
        if offset is not None and offset > 604800:
            self.notice.set("视频位置超出上限。")
            return False
        choice = self.decisions[self.current - 1]
        choice.update(kind=kind, evidence_path=self.evidence_path if kind != SKIP else None,
                      evidence_offset_s=offset if kind != SKIP else None)
        self.tree.set(str(self.current), "choice", DISPLAY.get(kind, "尚未选择"))
        self.notice.set("")
        return True

    def _select(self, _event=None):
        selected = self.tree.selection()
        if not selected or int(selected[0]) == self.current:
            return
        if self.current is not None and self._save() is False:
            self.tree.selection_set(str(self.current))
            return
        self.current = int(selected[0])
        choice = self.decisions[self.current - 1]
        self.kind.set(DISPLAY.get(choice["kind"], ""))
        offset = choice["evidence_offset_s"]
        self.offset.set(str(offset) if offset is not None else "")
        self.evidence_path = choice["evidence_path"]
        self.evidence.set(self.evidence_path.name if self.evidence_path else "尚未选择证据")

    def _choose_evidence(self):
        if self.current is None:
            return
        name = filedialog.askopenfilename(parent=self.window, title="选择已人工核对的本机服务证据")
        if name:
            self.evidence_path = Path(name)
            self.evidence.set(self.evidence_path.name)

    def _submit(self):
        if self._save() is False:
            return
        if (not self.attested.get() or any(not row["kind"] or (
                row["kind"] != SKIP and row["evidence_path"] is None) for row in self.decisions)):
            self.notice.set("请逐项选择；已审核项需要证据，并须勾选人工核对声明。")
            return
        if all(row["kind"] == SKIP for row in self.decisions):
            self.notice.set("至少需要一项经人工审核且有独立证据的服务记录。")
            return
        try:
            self.controller.export_tire_review(plan_sha256=self.plan["plan_sha256"],
                decisions=self.decisions, attested=self.attested.get())
        except Exception:
            self.notice.set("草稿已失效或后台忙；请保持离线，重新准备审核。")
        else:
            self.window.destroy()
