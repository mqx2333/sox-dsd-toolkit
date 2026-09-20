"""
gui.py -- tkinter front-end for the DSD -> PCM highest-quality command builder (sox_ng).

Run:  python gui.py
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk
from typing import List, Optional

import core

APP_TITLE = "DSD → PCM 最高质量指令生成器 (sox_ng)"
PAD = 8


class App(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=PAD)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)

        self.dsd: Optional[core.DsdInfo] = None
        self.probe: dict = {}
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._busy = False

        self.var_sox = tk.StringVar()
        self.var_input = tk.StringVar()
        self.var_output = tk.StringVar()
        self.var_bits = tk.StringVar(value="24")
        self.var_rate = tk.StringVar(value="")
        self.var_container = tk.StringVar(value="wav")
        self.var_effects = tk.StringVar(value="")
        self.var_status = tk.StringVar(value="就绪。先选择 DSD 源文件。")

        self._build()
        self._detect_sox()
        self.after(120, self._drain)

    # ------------------------------------------------------------------ UI
    def _section(self, text: str, row: int) -> None:
        ttk.Label(self, text=text, font=tkfont.Font(weight="bold"))\
            .grid(row=row, column=0, columnspan=3, sticky="w", pady=(PAD + 2, 2))

    def _build(self) -> None:
        row = 0
        self._section("1. sox_ng", row); row += 1
        ttk.Label(self, text="sox_ng.exe").grid(row=row, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.var_sox).grid(row=row, column=1, sticky="ew")
        ttk.Button(self, text="浏览…", command=self._pick_sox)\
            .grid(row=row, column=2, sticky="w", padx=(PAD, 0)); row += 1
        self.lbl_sox = ttk.Label(self, text="检测中…", foreground="#666")
        self.lbl_sox.grid(row=row, column=0, columnspan=3, sticky="w"); row += 1

        self._section("2. 文件", row); row += 1
        ttk.Label(self, text="DSD 源文件 (.dsf/.dff)").grid(row=row, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.var_input).grid(row=row, column=1, sticky="ew")
        ttk.Button(self, text="浏览…", command=self._pick_input).grid(row=row, column=2, sticky="w", padx=(PAD, 0))
        row += 1
        ttk.Label(self, text="输出 PCM 文件").grid(row=row, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.var_output).grid(row=row, column=1, sticky="ew")
        ttk.Button(self, text="浏览…", command=self._pick_output).grid(row=row, column=2, sticky="w", padx=(PAD, 0))
        row += 1
        self.lbl_input = ttk.Label(self, text="尚未读取 DSD 头信息。", justify="left", foreground="#666")
        self.lbl_input.grid(row=row, column=0, columnspan=3, sticky="w"); row += 1

        self._section("3. 质量设置（默认即最高质量）", row); row += 1
        line = ttk.Frame(self); line.grid(row=row, column=0, columnspan=3, sticky="ew")
        ttk.Label(line, text="位深").pack(side="left")
        ttk.Combobox(line, textvariable=self.var_bits, width=5, state="readonly",
                     values=["24", "16"]).pack(side="left", padx=(4, PAD * 2))
        ttk.Label(line, text="目标采样率 Hz").pack(side="left")
        self.cmb_rate = ttk.Combobox(line, textvariable=self.var_rate, width=10, state="readonly")
        self.cmb_rate.pack(side="left", padx=(4, PAD * 2))
        ttk.Label(line, text="容器").pack(side="left")
        ttk.Combobox(line, textvariable=self.var_container, width=6, state="readonly",
                     values=sorted(core.CONTAINERS)).pack(side="left", padx=(4, 0))
        row += 1

        line = ttk.Frame(self); line.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        ttk.Label(line, text="解码后追加效果（可留空，例如 gain -1 或 sinc 20-38000）").pack(side="left")
        ttk.Entry(line, textvariable=self.var_effects).pack(side="left", fill="x", expand=True, padx=(4, 0))
        row += 1

        self._section("4. 生成的指令（最高质量）", row); row += 1
        self.txt_cmd = tk.Text(self, height=4, wrap="char", font=("Consolas", 9))
        self.txt_cmd.grid(row=row, column=0, columnspan=3, sticky="ew"); row += 1
        bar = ttk.Frame(self); bar.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        tk.Button(bar, text="📋  一键复制指令", command=self._copy, font=tkfont.Font(weight="bold"),
                  padx=12, pady=4).pack(side="left")
        ttk.Button(bar, text="重新生成", command=self._refresh).pack(side="left", padx=(PAD, 0))
        self.btn_verify = ttk.Button(bar, text="验证产出（测带内电平）", command=self._start_verify)
        self.btn_verify.pack(side="left", padx=(PAD, 0))
        self.btn_run = ttk.Button(bar, text="直接执行转换（带进度条）", command=self._run_conversion)
        self.btn_run.pack(side="left", padx=(PAD, 0))
        self.lbl_copy = ttk.Label(bar, text="", foreground="#0a5")
        self.lbl_copy.pack(side="left", padx=(PAD, 0))
        row += 1

        self.pbar = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.pbar.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        row += 1

        self._section("5. 说明", row); row += 1
        self.txt_notes = tk.Text(self, height=11, wrap="word", font=("Microsoft YaHei UI", 9))
        self.txt_notes.grid(row=row, column=0, columnspan=3, sticky="ew")
        self.txt_notes.configure(state="disabled"); row += 1

        ttk.Label(self, textvariable=self.var_status, foreground="#0a5")\
            .grid(row=row, column=0, columnspan=3, sticky="w", pady=(PAD, 0))

        for v in (self.var_bits, self.var_rate, self.var_container, self.var_effects, self.var_output):
            v.trace_add("write", lambda *_: self._refresh())

    # -------------------------------------------------------------- actions
    def _pick_sox(self) -> None:
        p = filedialog.askopenfilename(title="选择 sox_ng.exe",
                                       filetypes=[("sox_ng", "*.exe"), ("所有文件", "*.*")])
        if p:
            self.var_sox.set(p)
            self._detect_sox()

    def _pick_input(self) -> None:
        p = filedialog.askopenfilename(title="选择 DSD 文件",
                                       filetypes=[("DSD", "*.dsf *.dff"), ("所有文件", "*.*")])
        if not p:
            return
        self.var_input.set(p)
        self.var_output.set(self._suggest_output(p))
        self._read_header()

    def _pick_output(self) -> None:
        ext = core.CONTAINERS.get(self.var_container.get(), ("", None, "wav"))[2]
        p = filedialog.asksaveasfilename(title="选择输出文件",
                                         initialdir=os.path.dirname(self.var_input.get() or "") or None,
                                         defaultextension="." + ext,
                                         filetypes=[("WAV", "*.wav"), ("FLAC", "*.flac")])
        if p:
            self.var_output.set(p)
            self.var_container.set("flac" if p.lower().endswith(".flac") else "wav")
            self._refresh()

    @staticmethod
    def _suggest_output(path: str) -> str:
        base, _ = os.path.splitext(path)
        return base + "_PCM.wav"

    def _detect_sox(self) -> None:
        if not self.var_sox.get():
            self.var_sox.set(core.find_sox() or "")
        exe = self.var_sox.get()
        if not exe or not os.path.isfile(exe):
            self.lbl_sox.configure(text="找不到 sox_ng。请手动指定（官方 SoX 14.4.2 读不了 DSD）。",
                                   foreground="#c00")
            self.probe = {}
            return
        self.probe = core.probe_sox(exe)
        bits = [str(self.probe.get("version", ""))]
        reads = bool(self.probe.get("reads_dsd"))
        is_ng = bool(self.probe.get("is_ng"))
        bits.append("DSD 读取: " + ("有" if reads else "无"))
        self.lbl_sox.configure(text="  |  ".join(bits), foreground="#0a5" if reads else "#c00")
        if not reads:
            self.var_status.set("这个 sox 读不了 DSD：官方 SoX 14.4.2 没有 DSD 支持，"
                                "请改用 sox_ng（14.6.0 起支持）。")
        self._refresh()

    def _read_header(self) -> None:
        path = self.var_input.get()
        if not path or not os.path.isfile(path):
            return
        self.dsd = core.read_dsd_info(path)
        d = self.dsd
        if not d.ok:
            self.lbl_input.configure(text="读取失败：%s" % d.error, foreground="#c00")
            self._refresh()
            return
        rec = d.recommended_target
        dur = d.duration_s
        self.lbl_input.configure(
            text="容器 %s | 采样率 %d Hz (%s) | %d 声道 | 1-bit | 时长 %s\n推荐目标：%d Hz / 24-bit"
                 % (d.container.upper(), d.rate or 0, d.tier, d.channels or 0,
                    ("%.1f s" % dur) if dur else "未知", rec or 0),
            foreground="#000")
        self._reload_rates()
        if rec:
            self.var_rate.set(str(rec))
        self._refresh()

    def _reload_rates(self) -> None:
        base = (self.dsd.rate // 32) if (self.dsd and self.dsd.rate) else None
        cands: List[int] = []
        if base:
            cands += [base // 2, base, base * 2, base * 4]
        cands += [44100, 88200, 176400, 352800, 48000, 96000, 192000, 384000]
        seen, vals = set(), []
        for c in cands:
            if c and c not in seen and c >= 8000:
                seen.add(c); vals.append(str(c))
        self.cmb_rate.configure(values=vals)

    # ------------------------------------------------------------ execute
    def _run_conversion(self) -> None:
        """Run the conversion through Convert-Dsd.ps1 so the GUI can show real progress."""
        plan = self._plan()
        if not plan:
            messagebox.showinfo(APP_TITLE, "先生成一条指令。")
            return
        if self._busy:
            return
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "Convert-Dsd.ps1")
        if not os.path.isfile(script):
            messagebox.showinfo(APP_TITLE, "找不到 Convert-Dsd.ps1（应与本目录同级的上级目录）。"
                                           "可改用「一键复制指令」到终端执行。")
            return
        self._busy = True
        self.btn_run.configure(state="disabled")
        self.btn_verify.configure(state="disabled")
        self.pbar.configure(value=0)
        self.var_status.set("转换中…")
        status_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_progress.json")
        self._watch = {"file": status_file, "mtime": 0.0, "proc": None}
        argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                "-Direction", "to-pcm", "-InputFile", plan.input_path,
                "-OutputFile", plan.output_path, "-Bits", str(plan.bits),
                "-StatusFile", status_file,
                "-Container", plan.container]
        threading.Thread(target=self._run_worker, args=(argv,), daemon=True).start()
        self.after(250, self._poll_progress)

    def _run_worker(self, argv: List[str]) -> None:
        try:
            import subprocess
            p = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._q.put(("run", p.returncode, (p.stderr or "")[-400:], None))
        except Exception as exc:
            self._q.put(("run", -1, str(exc), None))

    def _poll_progress(self) -> None:
        """Poll the JSON status file Convert-Dsd.ps1 writes and move the bar."""
        info = getattr(self, "_watch", None)
        if not info:
            return
        try:
            import json
            if os.path.isfile(info["file"]):
                mtime = os.path.getmtime(info["file"])
                if mtime != info["mtime"]:
                    info["mtime"] = mtime
                    with open(info["file"], "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                    pct = data.get("percent")
                    if pct is not None:
                        self.pbar.configure(value=float(pct))
                    self.var_status.set("转换中 %s%%  %s" % (pct, data.get("detail") or ""))
        except Exception:
            pass
        if self._busy:
            self.after(250, self._poll_progress)

    # ------------------------------------------------------------ verify
    def _start_verify(self) -> None:
        plan = self._plan()
        if not plan:
            messagebox.showinfo(APP_TITLE, "先生成一条指令。")
            return
        if not os.path.isfile(plan.output_path):
            messagebox.showinfo(APP_TITLE, "输出文件还不存在，先执行转换。")
            return
        if self._busy:
            return
        self._busy = True
        self.btn_verify.configure(state="disabled")
        self.btn_run.configure(state="disabled")
        self.var_status.set("正在测量产出…")
        threading.Thread(target=self._verify_worker,
                         args=(plan.output_path, self.var_sox.get()), daemon=True).start()

    def _verify_worker(self, path: str, sox: str) -> None:
        try:
            val = core.measure_inband(path, sox)
            self._q.put(("verify", val, path, None))
        except Exception as exc:
            self._q.put(("verify", None, path, str(exc)))

    def _drain(self) -> None:
        try:
            while True:
                kind, a, b, c = self._q.get_nowait()
                self._busy = False
                self.btn_verify.configure(state="normal")
                self.btn_run.configure(state="normal")
                if kind == "run":
                    if a == 0:
                        self.pbar.configure(value=100)
                        self.var_status.set("转换完成。可点「验证产出」测量带内电平。")
                    else:
                        self.pbar.configure(value=0)
                        self.var_status.set("转换失败（exit %s）：%s" % (a, (b or "").strip()[:200]))
                elif kind == "verify":
                    if a is None:
                        self.var_status.set("测量失败：%s" % (c or "无法读取产出"))
                    else:
                        self.var_status.set("产出 20 Hz–20 kHz 带内电平：%.2f dBFS"
                                            "（音乐内容即节目电平；若是静音探针则等于噪声底）" % a)
        except queue.Empty:
            pass
        self.after(150, self._drain)

    # ------------------------------------------------------- plan refresh
    def _plan(self) -> Optional[core.ConversionPlan]:
        sox = self.var_sox.get()
        src = self.var_input.get()
        dst = self.var_output.get()
        if not (sox and src and dst and self.dsd and self.dsd.ok):
            return None
        try:
            bits = int(self.var_bits.get())
        except ValueError:
            bits = 24
        try:
            rate = int(float(self.var_rate.get()))
        except ValueError:
            rate = self.dsd.recommended_target or 88200
        effects = [t for t in self.var_effects.get().replace(",", " ").split() if t]
        return core.build_plan(sox, src, dst, self.dsd, bits=bits, target_rate=rate,
                               container=self.var_container.get().lower(),
                               post_effects=effects,
                               ffmpeg=core.find_ffmpeg())

    def _refresh(self) -> None:
        plan = self._plan()
        if plan is None:
            text = "请选择 DSD 源文件与输出文件后自动生成指令。"
            notes: List[str] = []
        else:
            text = core.build_command(plan)
            notes = list(plan.notes) + [("⚠ " + w) for w in plan.warnings]
            notes.append("")
            notes.append("为什么输出速率是必需的：sox_ng 的 DSD 读取器按设计只把 1-bit 展开成"
                         "±满幅样本、速率标为 DSD 速率，PCM 转换交给普通 rate 重采样器；"
                         "SoX 只在输出速率与输入不同时才自动插入 rate。不给速率就会把 1-bit 流"
                         "当 8-bit PCM 原样写出（±1.0 方波、峰值 0.00 dBFS）。")
            notes.append("为什么 24-bit：实测 16-bit 让 DSD64/128/256 的带内噪声底分别劣化 "
                         "13.6 / 17.9 / 22.3 dB。")
            notes.append("为什么目标速率取 2 倍基率：24-bit 下目标速率对带内噪声底影响很小"
                         "（DSD64 在 44.1k–176.4k 间仅差 2.6 dB），取 2 倍是为了给重建滤镜留足"
                         "过渡带，避免为压超声噪声而用陡峭到会振铃的滤镜。")
            notes.append("质量等价性：六音探针（100 Hz–18 kHz）实测 sox_ng 与 ffmpeg 的解码"
                         "结果在每个音上差值均为 0.00 dB，逐频段光谱差 < 1 dB。")
        self.txt_cmd.configure(state="normal")
        self.txt_cmd.delete("1.0", "end")
        self.txt_cmd.insert("1.0", text)
        self.txt_cmd.configure(state="disabled")

        if plan:
            notes.append("验证指令：%s" % core.build_verify_command(plan))
        self.txt_notes.configure(state="normal")
        self.txt_notes.delete("1.0", "end")
        self.txt_notes.insert("1.0", "\n".join(notes))
        self.txt_notes.configure(state="disabled")

    def _copy(self) -> None:
        text = self.txt_cmd.get("1.0", "end").strip()
        if not text or text.startswith("请选择"):
            messagebox.showinfo(APP_TITLE, "还没有可复制的指令。")
            return
        if core.copy_to_clipboard(text):
            self.lbl_copy.configure(text="已复制到剪贴板 ✓")
            self.after(2500, lambda: self.lbl_copy.configure(text=""))
        else:
            messagebox.showwarning(APP_TITLE, "复制失败，请手动选中文本框内容复制。")


def main() -> None:
    root = tk.Tk()
    root.title(APP_TITLE)
    root.minsize(980, 740)
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
