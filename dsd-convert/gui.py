"""
gui.py -- unified PCM <-> DSD command builder.

Direction is detected from the input file's extension, and only the controls that
matter for that direction are shown.

Run:  python gui.py
"""

from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk
from typing import List, Optional

import core

APP_TITLE = "PCM ↔ DSD 指令生成器 (sox_ng)"
PAD = 8


class App(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=PAD)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)

        self.direction: Optional[str] = None
        self.dsd: Optional[core.DsdInfo] = None
        self.signal: Optional[core.SignalInfo] = None
        self.pcm_header: dict = {}
        self.probe: dict = {}
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._busy = False

        # tool paths + shared
        self.var_sox = tk.StringVar()
        self.var_ffmpeg = tk.StringVar()
        self.var_input = tk.StringVar()
        self.var_output = tk.StringVar()
        self.var_status = tk.StringVar(value="就绪。选择一个源文件，方向会自动判定。")
        # to-dsd
        self.var_gainmode = tk.StringVar(value="auto")
        self.var_gain = tk.StringVar(value="0.0")
        self.var_dsdref = tk.StringVar(value="0.0")
        self.var_margin = tk.StringVar(value="0.0")
        self.var_dsdrate = tk.StringVar(value="2822400")
        self.var_filter = tk.StringVar(value="sdm-6")
        self.var_torder = tk.StringVar(value="8")
        self.var_tpaths = tk.StringVar(value="16")
        self.var_tlat = tk.StringVar(value="512")
        # to-pcm
        self.var_bits = tk.StringVar(value="24")
        self.var_pcmrate = tk.StringVar(value="88200")
        self.var_container = tk.StringVar(value="wav")
        # both
        self.var_effects = tk.StringVar(value="")

        self._build()
        self._detect_tools()
        self.after(120, self._drain)

    # ------------------------------------------------------------------ UI
    def _section(self, text: str, row: int) -> None:
        ttk.Label(self, text=text, font=tkfont.Font(weight="bold"))\
            .grid(row=row, column=0, columnspan=3, sticky="w", pady=(PAD + 2, 2))

    def _build(self) -> None:
        row = 0
        self._section("1. 工具", row); row += 1
        ttk.Label(self, text="sox_ng").grid(row=row, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.var_sox).grid(row=row, column=1, sticky="ew")
        ttk.Button(self, text="浏览…", command=lambda: self._pick_exe(self.var_sox))\
            .grid(row=row, column=2, sticky="w", padx=(PAD, 0)); row += 1
        ttk.Label(self, text="ffmpeg（DST 用）").grid(row=row, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.var_ffmpeg).grid(row=row, column=1, sticky="ew")
        ttk.Button(self, text="浏览…", command=lambda: self._pick_exe(self.var_ffmpeg))\
            .grid(row=row, column=2, sticky="w", padx=(PAD, 0)); row += 1
        self.lbl_tools = ttk.Label(self, text="检测中…", foreground="#666")
        self.lbl_tools.grid(row=row, column=0, columnspan=3, sticky="w"); row += 1

        self._section("2. 文件（方向按扩展名自动判定）", row); row += 1
        ttk.Label(self, text="源文件").grid(row=row, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.var_input).grid(row=row, column=1, sticky="ew")
        ttk.Button(self, text="浏览…", command=self._pick_input)\
            .grid(row=row, column=2, sticky="w", padx=(PAD, 0)); row += 1
        ttk.Label(self, text="输出文件").grid(row=row, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.var_output).grid(row=row, column=1, sticky="ew")
        ttk.Button(self, text="浏览…", command=self._pick_output)\
            .grid(row=row, column=2, sticky="w", padx=(PAD, 0)); row += 1
        self.lbl_input = ttk.Label(self, text="尚未选择文件。", justify="left", foreground="#666")
        self.lbl_input.grid(row=row, column=0, columnspan=3, sticky="w"); row += 1

        # ---- direction-specific area: two frames, one shown at a time ----
        self.frm_dsd = ttk.LabelFrame(self, text=" PCM → DSD 设置 ", padding=PAD)
        self.frm_pcm = ttk.LabelFrame(self, text=" DSD → PCM 设置 ", padding=PAD)
        self.frm_dsd.grid(row=row, column=0, columnspan=3, sticky="ew")
        self.frm_pcm.grid(row=row, column=0, columnspan=3, sticky="ew")
        self._build_to_dsd(self.frm_dsd)
        self._build_to_pcm(self.frm_pcm)
        self.frm_dsd.grid_remove()
        self.frm_pcm.grid_remove()
        self.frm_slot = row
        row += 1

        self._section("3. 生成的指令（最高质量）", row); row += 1
        self.txt_cmd = tk.Text(self, height=4, wrap="char", font=("Consolas", 9))
        self.txt_cmd.grid(row=row, column=0, columnspan=3, sticky="ew"); row += 1
        bar = ttk.Frame(self); bar.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        tk.Button(bar, text="📋  一键复制指令", command=self._copy, font=tkfont.Font(weight="bold"),
                  padx=12, pady=4).pack(side="left")
        ttk.Button(bar, text="重新生成", command=self._refresh).pack(side="left", padx=(PAD, 0))
        ttk.Button(bar, text="测量源文件", command=self._start_measure).pack(side="left", padx=(PAD, 0))
        self.btn_run = ttk.Button(bar, text="直接执行转换（带进度条）", command=self._run_conversion)
        self.btn_run.pack(side="left", padx=(PAD, 0))
        self.btn_verify = ttk.Button(bar, text="验证产出", command=self._start_verify)
        self.btn_verify.pack(side="left", padx=(PAD, 0))
        self.lbl_copy = ttk.Label(bar, text="", foreground="#0a5")
        self.lbl_copy.pack(side="left", padx=(PAD, 0))
        row += 1

        self.pbar = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.pbar.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(4, 0)); row += 1

        self._section("4. 说明", row); row += 1
        self.txt_notes = tk.Text(self, height=12, wrap="word", font=("Microsoft YaHei UI", 9))
        self.txt_notes.grid(row=row, column=0, columnspan=3, sticky="ew")
        self.txt_notes.configure(state="disabled"); row += 1

        ttk.Label(self, textvariable=self.var_status, foreground="#0a5")\
            .grid(row=row, column=0, columnspan=3, sticky="w", pady=(PAD, 0))

        for v in (self.var_bits, self.var_pcmrate, self.var_container, self.var_effects,
                  self.var_gain, self.var_gainmode, self.var_dsdref, self.var_margin,
                  self.var_dsdrate, self.var_filter, self.var_torder, self.var_tpaths,
                  self.var_tlat, self.var_output):
            v.trace_add("write", lambda *_: self._refresh())

    def _build_to_dsd(self, f: ttk.Frame) -> None:
        f.columnconfigure(1, weight=1)
        r = 0
        line = ttk.Frame(f); line.grid(row=r, column=0, columnspan=3, sticky="ew"); r += 1
        ttk.Label(line, text="DSD 目标速率").pack(side="left")
        self.cmb_dsdrate = ttk.Combobox(line, textvariable=self.var_dsdrate, width=10, state="readonly")
        self.cmb_dsdrate.pack(side="left", padx=(4, PAD * 2))
        ttk.Label(line, text="噪声整形滤波器").pack(side="left")
        ttk.Combobox(line, textvariable=self.var_filter, width=10, state="readonly",
                     values=list(core.FILTERS)).pack(side="left", padx=(4, 0))

        line = ttk.Frame(f); line.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(4, 0)); r += 1
        for text, var, width in (("trellis order", self.var_torder, 5),
                                 ("paths", self.var_tpaths, 5),
                                 ("latency", self.var_tlat, 6)):
            ttk.Label(line, text=text).pack(side="left")
            ttk.Entry(line, textvariable=var, width=width).pack(side="left", padx=(4, PAD * 2))

        line = ttk.Frame(f); line.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(4, 0)); r += 1
        ttk.Label(line, text="增益").pack(side="left")
        ttk.Radiobutton(line, text="自动计算", value="auto", variable=self.var_gainmode).pack(side="left", padx=(4, 4))
        ttk.Radiobutton(line, text="手动", value="manual", variable=self.var_gainmode).pack(side="left")
        ttk.Entry(line, textvariable=self.var_gain, width=8).pack(side="left", padx=(4, 4))
        ttk.Label(line, text="dB").pack(side="left")
        self.lbl_gain_why = ttk.Label(line, text="", foreground="#666")
        self.lbl_gain_why.pack(side="left", padx=(PAD, 0))

        line = ttk.Frame(f); line.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(4, 0)); r += 1
        ttk.Label(line, text="峰值目标 (dBDSD)").pack(side="left")
        ttk.Combobox(line, textvariable=self.var_dsdref, width=6, state="readonly",
                     values=["+3.1", "0.0", "-1.0", "-2.0", "-3.0"]).pack(side="left", padx=(4, PAD * 2))
        ttk.Label(line, text="额外安全余量 dB").pack(side="left")
        ttk.Entry(line, textvariable=self.var_margin, width=6).pack(side="left", padx=(4, 0))

        line = ttk.Frame(f); line.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(4, 0)); r += 1
        ttk.Label(line, text="输出后追加效果（如 trim 0 30）").pack(side="left")
        ttk.Entry(line, textvariable=self.var_effects).pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.entry_effects_dsd = line

    def _build_to_pcm(self, f: ttk.Frame) -> None:
        f.columnconfigure(1, weight=1)
        r = 0
        line = ttk.Frame(f); line.grid(row=r, column=0, columnspan=3, sticky="ew"); r += 1
        ttk.Label(line, text="位深").pack(side="left")
        ttk.Combobox(line, textvariable=self.var_bits, width=5, state="readonly",
                     values=["24", "16"]).pack(side="left", padx=(4, PAD * 2))
        ttk.Label(line, text="目标采样率 Hz").pack(side="left")
        self.cmb_pcmrate = ttk.Combobox(line, textvariable=self.var_pcmrate, width=10, state="readonly")
        self.cmb_pcmrate.pack(side="left", padx=(4, PAD * 2))
        ttk.Label(line, text="容器").pack(side="left")
        ttk.Combobox(line, textvariable=self.var_container, width=6, state="readonly",
                     values=sorted(core.TO_PCM_CONTAINERS)).pack(side="left", padx=(4, 0))

        line = ttk.Frame(f); line.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(4, 0)); r += 1
        ttk.Label(line, text="解码后追加效果（如 gain -1）").pack(side="left")
        ttk.Entry(line, textvariable=self.var_effects).pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.entry_effects_pcm = line

    # -------------------------------------------------------------- actions
    def _pick_exe(self, var: tk.StringVar) -> None:
        p = filedialog.askopenfilename(title="选择可执行文件",
                                       filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")])
        if p:
            var.set(p)
            self._detect_tools()

    def _pick_input(self) -> None:
        p = filedialog.askopenfilename(
            title="选择源文件（PCM 或 DSD）",
            filetypes=[("音频文件", "*.flac *.wav *.aif *.aiff *.dsf *.dff *.m4a *.ape *.wv"),
                       ("DSD", "*.dsf *.dff"), ("PCM", "*.flac *.wav *.aiff"), ("所有文件", "*.*")])
        if not p:
            return
        self.var_input.set(p)
        self._on_input_changed()

    def _pick_output(self) -> None:
        direction = self.direction or "to-pcm"
        if direction == "to-dsd":
            types = [("DSF", "*.dsf"), ("DFF", "*.dff")]
        else:
            types = [("WAV", "*.wav"), ("FLAC", "*.flac")]
        p = filedialog.asksaveasfilename(title="选择输出文件",
                                         initialdir=os.path.dirname(self.var_input.get() or "") or None,
                                         defaultextension=types[0][1].lstrip("*"),
                                         filetypes=types)
        if p:
            self.var_output.set(p)
            self._refresh()

    def _on_input_changed(self) -> None:
        src = self.var_input.get()
        if not src or not os.path.isfile(src):
            return
        self.direction = core.detect_direction(src)
        if self.direction is None:
            self.lbl_input.configure(text="无法从扩展名判定方向。请选择 PCM 或 DSD 文件。",
                                     foreground="#c00")
            self._show_direction(None)
            return
        self.var_output.set(core.suggested_output(
            src, self.direction,
            self.var_container.get().lower() if self.direction == "to-pcm" else None))
        self._show_direction(self.direction)
        self._start_measure()

    def _show_direction(self, direction: Optional[str]) -> None:
        self.frm_dsd.grid_remove()
        self.frm_pcm.grid_remove()
        if direction == "to-dsd":
            self.frm_dsd.grid()
        elif direction == "to-pcm":
            self.frm_pcm.grid()
        self._reload_rates()

    def _detect_tools(self) -> None:
        if not self.var_sox.get():
            self.var_sox.set(core.find_sox() or "")
        if not self.var_ffmpeg.get():
            self.var_ffmpeg.set(core.find_ffmpeg() or "")
        bits: List[str] = []
        exe = self.var_sox.get()
        if exe and os.path.isfile(exe):
            self.probe = core.probe_sox(exe)
            bits.append(str(self.probe.get("version", "")))
            bits.append("读 DSD: " + ("有" if self.probe.get("reads_dsd") else "无"))
            bits.append("sdm: " + ("有" if self.probe.get("has_sdm") else "无"))
        else:
            self.probe = {}
            bits.append("sox_ng: 未找到")
        ff = self.var_ffmpeg.get()
        bits.append("ffmpeg: " + ("已找到" if (ff and os.path.isfile(ff)) else "未找到"))
        ok = bool(self.probe.get("reads_dsd")) and bool(self.probe.get("has_sdm"))
        self.lbl_tools.configure(text="  |  ".join(bits), foreground="#0a5" if ok else "#c00")
        self._refresh()

    def _reload_rates(self) -> None:
        if self.direction == "to-pcm":
            vals = core.target_rate_choices(self.dsd, "to-pcm")
            self.cmb_pcmrate.configure(values=vals)
            if self.var_pcmrate.get() not in vals and vals:
                self.var_pcmrate.set(vals[0])
        else:
            vals = core.target_rate_choices(self.dsd, "to-dsd")
            self.cmb_dsdrate.configure(values=vals)
            if self.var_dsdrate.get() not in vals and vals:
                self.var_dsdrate.set(vals[0])

    # ------------------------------------------------------------ measuring
    def _start_measure(self) -> None:
        exe = self.var_sox.get()
        src = self.var_input.get()
        if not exe or not os.path.isfile(exe):
            messagebox.showerror(APP_TITLE, "请先指定有效的 sox_ng 路径。")
            return
        if not src or not os.path.isfile(src):
            messagebox.showerror(APP_TITLE, "请先选择存在的源文件。")
            return
        self.var_status.set("测量中…")
        threading.Thread(target=self._measure_worker, args=(exe, src, self.direction),
                         daemon=True).start()

    def _measure_worker(self, exe: str, src: str, direction: Optional[str]) -> None:
        try:
            if direction == "to-pcm":
                info = core.read_dsd_info(src)
                self._q.put(("dsd", info, None, None))
            else:
                hdr = core.read_pcm_header(src, exe)
                sig = core.measure(src, exe)
                self._q.put(("pcm", hdr, sig, None))
        except Exception as exc:
            self._q.put(("err", str(exc), None, None))

    def _drain(self) -> None:
        try:
            while True:
                kind, a, b, c = self._q.get_nowait()
                self._busy = False
                self.btn_run.configure(state="normal")
                self.btn_verify.configure(state="normal")
                if kind == "dsd":
                    self.dsd = a
                    self.pcm_header = {}
                    self._show_dsd(a)
                elif kind == "pcm":
                    self.pcm_header, self.signal = a, b
                    self.dsd = None
                    self._show_pcm(a, b)
                elif kind == "err":
                    self.lbl_input.configure(text="测量失败：%s" % a, foreground="#c00")
                elif kind == "run":
                    if a == 0:
                        self.pbar.configure(value=100)
                        self.var_status.set("转换完成。")
                    else:
                        self.pbar.configure(value=0)
                        self.var_status.set("转换失败（exit %s）：%s" % (a, (b or "").strip()[:180]))
                elif kind == "verify":
                    if a is None:
                        self.var_status.set("测量失败：%s" % (c or "无法读取产出"))
                    else:
                        self.var_status.set("产出 20 Hz–20 kHz 带内电平：%.2f dBFS" % a)
                self._refresh()
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _show_dsd(self, d: core.DsdInfo) -> None:
        if not d.ok:
            self.lbl_input.configure(text="读取失败：%s" % d.error, foreground="#c00")
            return
        rec = d.recommended_target
        self.lbl_input.configure(
            text="[DSD → PCM] 容器 %s%s | 采样率 %d Hz (%s) | %d 声道 | 时长 %s\n推荐目标：%d Hz / 24-bit"
                 % (d.container.upper(), " (DST 压缩)" if d.is_dst else "", d.rate or 0, d.tier,
                    d.channels or 0, ("%.1f s" % d.duration_s) if d.duration_s else "未知", rec or 0),
            foreground="#000")
        self._reload_rates()
        if rec:
            self.var_pcmrate.set(str(rec))

    def _show_pcm(self, hdr: dict, sig: core.SignalInfo) -> None:
        if not sig.ok:
            self.lbl_input.configure(text="[PCM → DSD] 测量失败：%s" % sig.error, foreground="#c00")
            return
        rate = hdr.get("rate")
        fam = core.classify_family(rate if isinstance(rate, int) else None)
        fam_txt = {"44.1k": "44.1k 家族", "48k": "48k 家族", "other": "其他", "unknown": "未知"}[fam]
        txt = ("[PCM → DSD] %d Hz / %s / %s bits | 峰值 %.2f dBFS" % (rate or 0, fam_txt,
               hdr.get("precision") or "?", sig.peak_db if sig.peak_db is not None else float("nan")))
        if sig.rms_db is not None:
            txt += " | RMS %.2f dBFS" % sig.rms_db
        if sig.clipped_source:
            txt += "\n⚠ 峰值已顶到满刻度：母带可能已削波，增益能保护调制器但救不回削波"
        self.lbl_input.configure(text=txt, foreground="#000")
        self._reload_rates()

    # --------------------------------------------------- run / verify
    def _run_conversion(self) -> None:
        plan = self._plan()
        if not plan:
            messagebox.showinfo(APP_TITLE, "先生成一条指令。")
            return
        if self._busy:
            return
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "Convert-Dsd.ps1")
        if not os.path.isfile(script):
            messagebox.showinfo(APP_TITLE, "找不到 Convert-Dsd.ps1（应在本目录的上级目录）。"
                                           "可改用「一键复制指令」。")
            return
        self._busy = True
        self.btn_run.configure(state="disabled")
        self.btn_verify.configure(state="disabled")
        self.pbar.configure(value=0)
        self.var_status.set("转换中…")
        status_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_progress.json")
        self._watch = {"file": status_file, "mtime": 0.0}
        argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                "-Direction", plan.direction,
                "-InputFile", plan.input_path, "-OutputFile", plan.output_path,
                "-StatusFile", status_file, "-Bits", str(plan.bits)]
        if plan.direction == "to-dsd":
            argv += ["-DsdRate", str(plan.dsd_rate)]
        else:
            argv += ["-DsdRate", str(plan.pcm_rate)]
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
        info = getattr(self, "_watch", None)
        if not info:
            return
        try:
            if os.path.isfile(info["file"]):
                mtime = os.path.getmtime(info["file"])
                if mtime != info["mtime"]:
                    info["mtime"] = mtime
                    with open(info["file"], "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                    if data.get("percent") is not None:
                        self.pbar.configure(value=float(data["percent"]))
                    self.var_status.set("转换中 %s%%  %s" % (data.get("percent"), data.get("detail") or ""))
        except Exception:
            pass
        if self._busy:
            self.after(250, self._poll_progress)

    def _start_verify(self) -> None:
        plan = self._plan()
        if not plan:
            return
        if not os.path.isfile(plan.output_path):
            messagebox.showinfo(APP_TITLE, "输出文件还不存在，先执行转换。")
            return
        self.var_status.set("正在测量产出…")
        threading.Thread(target=self._verify_worker,
                         args=(plan.output_path, self.var_sox.get()), daemon=True).start()

    def _verify_worker(self, path: str, sox: str) -> None:
        try:
            self._q.put(("verify", core.measure_inband(path, sox), path, None))
        except Exception as exc:
            self._q.put(("verify", None, path, str(exc)))

    # ------------------------------------------------------- plan refresh
    def _f(self, var: tk.StringVar, default: float) -> float:
        try:
            return float(var.get())
        except Exception:
            return default

    def _i(self, var: tk.StringVar, default: int, lo: int, hi: int) -> int:
        try:
            val = int(float(var.get()))
        except Exception:
            return default
        return max(lo, min(hi, val))

    def _plan(self) -> Optional[core.Plan]:
        src, dst, sox = self.var_input.get(), self.var_output.get(), self.var_sox.get()
        if not (src and dst and sox and self.direction):
            return None
        effects = [t for t in self.var_effects.get().replace(",", " ").split() if t]
        if self.direction == "to-pcm":
            if not (self.dsd and self.dsd.ok):
                return None
            return core.plan_to_pcm(sox, self.var_ffmpeg.get() or core.find_ffmpeg(),
                                    src, dst, self.dsd,
                                    bits=int(self.var_bits.get() or 24),
                                    pcm_rate=self._i(self.var_pcmrate, 88200, 8000, 705600),
                                    post_effects=effects)
        rate = self.pcm_header.get("rate")
        return core.plan_to_dsd(
            sox, src, dst, rate if isinstance(rate, int) else None,
            self.signal.peak_db if (self.signal and self.signal.ok) else None,
            gain_mode=self.var_gainmode.get(),
            gain_db=self._f(self.var_gain, 0.0),
            target_peak_dbfs=self._f(self.var_dsdref, 0.0) - 6.0,
            safety_margin_db=self._f(self.var_margin, 0.0),
            dsd_rate=self._i(self.var_dsdrate, 2822400, 8000, 22579200),
            filter_name=self.var_filter.get(),
            trellis_order=self._i(self.var_torder, 8, 3, 32),
            trellis_paths=self._i(self.var_tpaths, 16, 4, 32),
            trellis_latency=self._i(self.var_tlat, 512, 100, 2048),
            post_effects=effects)

    def _refresh(self) -> None:
        plan = self._plan()
        if plan is None:
            text = "选择源文件后自动生成指令。"
            notes: List[str] = []
        else:
            text = core.build_command(plan)
            notes = list(plan.notes) + [("⚠ " + w) for w in plan.warnings]
            notes.append("")
            if plan.direction == "to-dsd":
                if self.var_gainmode.get() != "manual":
                    self.var_gain.set("%.1f" % plan.gain_db)
                peak = plan.measured_peak_db
                self.lbl_gain_why.configure(
                    text=("= 目标 %.1f dBFS − 实测峰值 %.2f dBFS" % (plan.target_peak_dbfs, peak))
                    if peak is not None else "（尚未测量，此为假定值）",
                    foreground="#666" if peak is not None else "#c60")
                notes.append("为什么需要增益：sox 的 sdm 内部把输入乘 0.5，PCM 0 dBFS 等于调制器满幅，"
                             "而其稳定上限约 ±0.71（= +3.1 dBDSD，SACD 峰值上限）。")
                notes.append("为什么必须给 -f：不指定时 sox 会退化成最低阶的 clans-4，带内噪声明显变差。")
            else:
                notes.append("为什么输出速率必需：sox_ng 的 DSD 读取器只把 1-bit 展开成 ±满幅样本、"
                             "速率标为 DSD 速率，PCM 转换交给普通 rate 重采样器；不给速率就会输出方波。")
                notes.append("为什么 24-bit：实测 16-bit 让 DSD64/128/256 的带内噪声底分别劣化 "
                             "13.6 / 17.9 / 22.3 dB。")
                notes.append("为什么目标取 2 倍基率：24-bit 下目标速率对带内噪声底影响很小"
                             "（DSD64 在 44.1k–176.4k 间仅差 2.6 dB），取 2 倍是为给重建滤镜留过渡带。")
                if plan.tool == "ffmpeg":
                    notes.append("该文件是 DST 压缩的 DFF，sox_ng 读不了，故用 ffmpeg；"
                                 "两者解码质量实测等价（六音探针差值 0.00 dB）。")
        self.txt_cmd.configure(state="normal")
        self.txt_cmd.delete("1.0", "end")
        self.txt_cmd.insert("1.0", text)
        self.txt_cmd.configure(state="disabled")
        self.txt_notes.configure(state="normal")
        self.txt_notes.delete("1.0", "end")
        self.txt_notes.insert("1.0", "\n".join(notes))
        self.txt_notes.configure(state="disabled")

    def _copy(self) -> None:
        text = self.txt_cmd.get("1.0", "end").strip()
        if not text or text.startswith("选择源文件"):
            messagebox.showinfo(APP_TITLE, "还没有可复制的指令。")
            return
        if core.copy_to_clipboard(text):
            self.lbl_copy.configure(text="已复制 ✓")
            self.after(2500, lambda: self.lbl_copy.configure(text=""))
        else:
            messagebox.showwarning(APP_TITLE, "复制失败，请手动选中复制。")


def main() -> None:
    root = tk.Tk()
    root.title(APP_TITLE)
    root.minsize(1020, 780)
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
