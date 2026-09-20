"""
gui.py -- tkinter front-end for the PCM -> DSD highest-quality command builder.

Run:  python gui.py
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk
from typing import Dict, List, Optional

import core

APP_TITLE = "PCM → DSD 最高质量指令生成器 (sox_ng)"
PAD = 8


class App(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=PAD)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)

        self.sox_probe: Dict[str, object] = {}
        self.signal: Optional[core.SignalInfo] = None
        self.header: Dict[str, object] = {}
        self.container_rates: Dict[str, List[int]] = {}
        self._measure_queue: "queue.Queue[tuple]" = queue.Queue()
        self._measuring = False

        # ---- state ----
        self.var_sox = tk.StringVar()
        self.var_input = tk.StringVar()
        self.var_output = tk.StringVar()
        self.var_target = tk.StringVar(value="5644800")
        self.var_filter = tk.StringVar(value="sdm-6")
        self.var_torder = tk.StringVar(value="8")
        self.var_tpaths = tk.StringVar(value="16")
        self.var_tlat = tk.StringVar(value="512")
        self.var_gainmode = tk.StringVar(value="auto")
        self.var_gain = tk.StringVar(value="0.0")
        self.var_dsd_ref = tk.StringVar(value="0.0")
        self.var_margin = tk.StringVar(value="0.0")
        self.var_extreme = tk.BooleanVar(value=True)
        self.var_progress = tk.BooleanVar(value=True)
        self.var_noclobber = tk.BooleanVar(value=True)
        self.var_status = tk.StringVar(value="就绪。先选择源文件。")

        self._build()
        self._detect_sox()
        self.after(120, self._drain_queue)

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        row = 0
        self._section("1. sox_ng", row); row += 1
        row = self._row_sox(row)

        self._section("2. 文件", row); row += 1
        row = self._row_file(row, "源文件 (FLAC/WAV/…)", self.var_input, self._pick_input)
        row = self._row_file(row, "输出文件 (.dsf/.dff)", self.var_output, self._pick_output)

        self._section("3. 源文件测量（决定增益与是否重采样）", row); row += 1
        row = self._row_measure(row)

        self._section("4. 质量设置（默认即最高质量）", row); row += 1
        row = self._row_quality(row)

        self._section("5. 生成的指令（最高质量 + 已计算增益）", row); row += 1
        row = self._row_command(row)

        self._section("6. 说明", row); row += 1
        row = self._row_notes(row)

        ttk.Label(self, textvariable=self.var_status, foreground="#0a5")\
            .grid(row=row, column=0, columnspan=3, sticky="w", pady=(PAD, 0))

    def _section(self, text: str, row: int) -> None:
        lbl = ttk.Label(self, text=text, font=tkfont.Font(weight="bold"))
        lbl.grid(row=row, column=0, columnspan=3, sticky="w", pady=(PAD + 2, 2))

    def _row_sox(self, row: int) -> int:
        ttk.Entry(self, textvariable=self.var_sox).grid(row=row, column=0, columnspan=2, sticky="ew")
        ttk.Button(self, text="浏览…", command=self._pick_sox).grid(row=row, column=2, sticky="w", padx=(PAD, 0))
        row += 1
        self.lbl_sox = ttk.Label(self, text="正在检测…", foreground="#666")
        self.lbl_sox.grid(row=row, column=0, columnspan=3, sticky="w")
        return row + 1

    def _row_file(self, row: int, label: str, var: tk.StringVar, cmd) -> int:
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w")
        ttk.Entry(self, textvariable=var).grid(row=row, column=1, sticky="ew")
        ttk.Button(self, text="浏览…", command=cmd).grid(row=row, column=2, sticky="w", padx=(PAD, 0))
        return row + 1

    def _row_measure(self, row: int) -> int:
        bar = ttk.Frame(self)
        bar.grid(row=row, column=0, columnspan=3, sticky="ew")
        self.btn_measure = ttk.Button(bar, text="测量源文件", command=self._start_measure)
        self.btn_measure.pack(side="left")
        self.btn_remeasure = ttk.Button(bar, text="重新测量（可勾选真峰）", command=lambda: self._start_measure(True))
        self.btn_remeasure.pack(side="left", padx=(PAD, 0))
        row += 1
        self.lbl_measure = ttk.Label(self, text="尚未测量。", justify="left")
        self.lbl_measure.grid(row=row, column=0, columnspan=3, sticky="w")
        return row + 1

    def _row_quality(self, row: int) -> int:
        # target rate + filter
        line = ttk.Frame(self)
        line.grid(row=row, column=0, columnspan=3, sticky="ew")
        ttk.Label(line, text="DSD 目标速率").pack(side="left")
        self.cmb_target = ttk.Combobox(line, textvariable=self.var_target, width=12, state="readonly")
        self.cmb_target.pack(side="left", padx=(4, PAD * 2))
        ttk.Label(line, text="噪声整形滤波器").pack(side="left")
        ttk.Combobox(line, textvariable=self.var_filter, width=10, state="readonly",
                     values=list(core.FILTERS)).pack(side="left", padx=(4, 0))
        row += 1

        line = ttk.Frame(self)
        line.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        for text, var, width in (("trellis order", self.var_torder, 5),
                                 ("paths", self.var_tpaths, 5),
                                 ("latency", self.var_tlat, 6)):
            ttk.Label(line, text=text).pack(side="left")
            ttk.Entry(line, textvariable=var, width=width).pack(side="left", padx=(4, PAD * 2))
        ttk.Checkbutton(line, text="重采样极限参数 (-b 99 -d 33)", variable=self.var_extreme).pack(side="left")
        row += 1

        # gain
        line = ttk.Frame(self)
        line.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        ttk.Label(line, text="增益").pack(side="left")
        ttk.Radiobutton(line, text="自动计算", value="auto", variable=self.var_gainmode,
                        command=self._refresh).pack(side="left", padx=(4, 4))
        ttk.Radiobutton(line, text="手动", value="manual", variable=self.var_gainmode,
                        command=self._refresh).pack(side="left")
        ttk.Entry(line, textvariable=self.var_gain, width=8).pack(side="left", padx=(4, PAD * 2))
        ttk.Label(line, text="dB").pack(side="left")
        self.lbl_gain_why = ttk.Label(line, text="", foreground="#666")
        self.lbl_gain_why.pack(side="left", padx=(PAD, 0))
        row += 1

        line = ttk.Frame(self)
        line.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        ttk.Label(line, text="峰值目标 (dBDSD)").pack(side="left")
        ttk.Combobox(line, textvariable=self.var_dsd_ref, width=6, state="readonly",
                     values=["+3.1", "0.0", "-1.0", "-2.0", "-3.0"]).pack(side="left", padx=(4, PAD * 2))
        ttk.Label(line, text="额外安全余量 dB").pack(side="left")
        ttk.Entry(line, textvariable=self.var_margin, width=6).pack(side="left", padx=(4, PAD * 2))
        row += 1

        line = ttk.Frame(self)
        line.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        ttk.Checkbutton(line, text="显示进度 -S", variable=self.var_progress).pack(side="left")
        ttk.Checkbutton(line, text="防覆盖 --no-clobber", variable=self.var_noclobber).pack(side="left", padx=(PAD, 0))
        row += 1

        # live-update the command whenever a setting changes
        for var in (self.var_target, self.var_filter, self.var_torder, self.var_tpaths,
                    self.var_tlat, self.var_gain, self.var_dsd_ref, self.var_margin,
                    self.var_extreme, self.var_progress, self.var_noclobber, self.var_gainmode):
            var.trace_add("write", lambda *_: self._refresh())
        return row

    def _row_command(self, row: int) -> int:
        self.txt_cmd = tk.Text(self, height=4, wrap="char", font=("Consolas", 9))
        self.txt_cmd.grid(row=row, column=0, columnspan=3, sticky="ew")
        row += 1
        bar = ttk.Frame(self)
        bar.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self.btn_copy = tk.Button(bar, text="📋  一键复制指令", command=self._copy,
                                  font=tkfont.Font(weight="bold"), padx=12, pady=4)
        self.btn_copy.pack(side="left")
        ttk.Button(bar, text="重新生成", command=self._refresh).pack(side="left", padx=(PAD, 0))
        self.lbl_copy = ttk.Label(bar, text="", foreground="#0a5")
        self.lbl_copy.pack(side="left", padx=(PAD, 0))
        return row + 1

    def _row_notes(self, row: int) -> int:
        self.txt_notes = tk.Text(self, height=9, wrap="word", font=("Microsoft YaHei UI", 9))
        self.txt_notes.grid(row=row, column=0, columnspan=3, sticky="ew")
        self.txt_notes.configure(state="disabled")
        return row + 1

    # -------------------------------------------------------------- actions
    def _pick_sox(self) -> None:
        path = filedialog.askopenfilename(title="选择 sox_ng.exe",
                                          filetypes=[("sox", "*.exe"), ("所有文件", "*.*")])
        if path:
            self.var_sox.set(path)
            self._detect_sox()

    def _pick_input(self) -> None:
        path = filedialog.askopenfilename(
            title="选择源文件",
            filetypes=[("音频文件", "*.flac *.wav *.aif *.aiff *.alac *.m4a *.ape *.wv *.ogg *.mp3"),
                       ("所有文件", "*.*")])
        if not path:
            return
        self.var_input.set(path)
        self.var_output.set(self._suggest_output(path))
        self._start_measure()

    def _pick_output(self) -> None:
        initial = os.path.dirname(self.var_output.get() or self.var_input.get() or "")
        path = filedialog.asksaveasfilename(title="选择输出文件", initialdir=initial or None,
                                           defaultextension=".dsf",
                                           filetypes=[("DSF", "*.dsf"), ("DFF", "*.dff")])
        if path:
            self.var_output.set(path)
            self._refresh()

    @staticmethod
    def _suggest_output(path: str) -> str:
        base, _ = os.path.splitext(path)
        return base + "_DSD.dsf"

    def _detect_sox(self) -> None:
        exe = core.find_sox(self.var_sox.get() or None)
        if not exe:
            self.lbl_sox.configure(text="找不到 sox_ng。请手动指定路径。", foreground="#c00")
            self.sox_probe = {}
            return
        self.var_sox.set(exe)
        self.sox_probe = core.probe_sox(exe)
        ok = bool(self.sox_probe.get("has_sdm"))
        containers = self.sox_probe.get("containers") or {}
        self.container_rates = {k: list(v) for k, v in containers.items()}  # type: ignore[union-attr]

        parts = [str(self.sox_probe.get("version", ""))]
        parts.append("sdm: " + ("有" if ok else "缺失"))
        if containers:
            parts.append("容器: " + ", ".join(sorted(containers)))
        self.lbl_sox.configure(text="  |  ".join(parts), foreground="#0a5" if ok else "#c00")

        self._reload_targets()
        self._refresh()

    def _reload_targets(self) -> None:
        ext = os.path.splitext(self.var_output.get())[1].lstrip(".").lower() or "dsf"
        rates = core.target_rates_for(self.container_rates, ext)
        values = ["%d" % r for r in rates]
        self.cmb_target.configure(values=values)
        if self.var_target.get() not in values:
            self.var_target.set(values[0] if values else "5644800")

    # ------------------------------------------------------------ measuring
    def _start_measure(self, truepeak: bool = False) -> None:
        exe = self.var_sox.get()
        src = self.var_input.get()
        if not exe or not os.path.isfile(exe):
            messagebox.showerror(APP_TITLE, "请先指定有效的 sox_ng 路径。")
            return
        if not src or not os.path.isfile(src):
            messagebox.showerror(APP_TITLE, "请先选择存在的源文件。")
            return
        if self._measuring:
            return
        self._measuring = True
        self.btn_measure.configure(state="disabled")
        self.btn_remeasure.configure(state="disabled")
        self.lbl_measure.configure(text="测量中…" + ("（真峰检测需升采样，较慢）" if truepeak else ""))
        threading.Thread(target=self._measure_worker, args=(exe, src, truepeak), daemon=True).start()

    def _measure_worker(self, exe: str, src: str, truepeak: bool) -> None:
        try:
            hdr, info, tp = core.measure_all(src, exe, truepeak=truepeak)
            self._measure_queue.put(("ok", hdr, info, tp))
        except Exception as exc:  # pragma: no cover - defensive
            self._measure_queue.put(("err", str(exc), None, None))

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, a, b, c = self._measure_queue.get_nowait()
                self._measuring = False
                self.btn_measure.configure(state="normal")
                self.btn_remeasure.configure(state="normal")
                if kind == "ok":
                    self.header, self.signal = a, b
                    self._truepeak = c
                    self._show_measurement()
                else:
                    self.lbl_measure.configure(text="测量失败：%s" % a, foreground="#c00")
                    self._refresh()
        except queue.Empty:
            pass
        self.after(120, self._drain_queue)

    def _show_measurement(self) -> None:
        info = self.signal
        if info is None:
            return
        if not info.ok:
            self.lbl_measure.configure(text="测量失败：%s" % info.error, foreground="#c00")
            self._refresh()
            return
        rate = self.header.get("rate")
        fam = core.classify_family(rate if isinstance(rate, int) else None)
        fam_txt = {"44.1k": "44.1k 家族", "48k": "48k 家族", "other": "其他", "unknown": "未知"}[fam]
        bits: List[str] = []
        if info.peak_db is not None:
            bits.append("样本峰值 %.2f dBFS" % info.peak_db)
        if info.peak_db_left is not None and info.peak_db_right is not None:
            bits.append("(L %.2f / R %.2f)" % (info.peak_db_left, info.peak_db_right))
        tp = getattr(self, "_truepeak", None)
        if tp is not None and tp.ok and tp.peak_db is not None and info.peak_db is not None:
            bits.append("4× 升采样真峰 %.2f dBFS (Δ%+.2f dB)" % (tp.peak_db, tp.peak_db - info.peak_db))
        if info.rms_db is not None:
            bits.append("RMS %.2f dBFS" % info.rms_db)
        if info.dc_offset is not None:
            bits.append("DC %.5f" % info.dc_offset)
        head = "%d Hz / %s / %s bits" % (rate or 0, fam_txt, self.header.get("precision") or "?")
        self.lbl_measure.configure(text=head + "\n" + " | ".join(bits), foreground="#000")
        self._refresh()

    # ------------------------------------------------------- plan refresh
    def _parse_float(self, var: tk.StringVar, default: float) -> float:
        try:
            return float(var.get())
        except Exception:
            return default

    def _parse_int(self, var: tk.StringVar, default: int, lo: int, hi: int) -> int:
        try:
            val = int(float(var.get()))
        except Exception:
            return default
        return max(lo, min(hi, val))

    def _current_plan(self) -> Optional[core.ConversionPlan]:
        exe = self.var_sox.get()
        src = self.var_input.get()
        dst = self.var_output.get()
        if not exe or not src or not dst:
            return None
        try:
            target = int(float(self.var_target.get()))
        except Exception:
            target = 5644800
        rate = self.header.get("rate")
        rate_int = rate if isinstance(rate, int) else None

        peak = None
        tp = getattr(self, "_truepeak", None)
        if tp is not None and tp.ok and tp.peak_db is not None:
            peak = max(tp.peak_db, self.signal.peak_db) if (self.signal and self.signal.peak_db is not None) else tp.peak_db
        elif self.signal and self.signal.ok:
            peak = self.signal.peak_db

        target_peak_dbfs = self._parse_float(self.var_dsd_ref, 0.0) - 6.0
        margin = self._parse_float(self.var_margin, 0.0)

        plan = core.build_plan(
            sox=exe, input_path=src, output_path=dst,
            source_rate=rate_int, target_rate=target,
            target_peak_dbfs=target_peak_dbfs,
            safety_margin_db=margin,
            measured_peak_db=peak,
            filter_name=self.var_filter.get(),
            trellis_order=self._parse_int(self.var_torder, 8, 3, 32),
            trellis_paths=self._parse_int(self.var_tpaths, 16, 4, 32),
            trellis_latency=self._parse_int(self.var_tlat, 512, 100, 2048),
            extreme_rate_opts=bool(self.var_extreme.get()),
            show_progress=bool(self.var_progress.get()),
            no_clobber=bool(self.var_noclobber.get()),
        )
        if self.var_gainmode.get() == "manual":
            auto = core.compute_gain(peak, target_peak_dbfs, margin)
            plan.gain_db = round(self._parse_float(self.var_gain, 0.0), 1)
            plan.warnings.append("手动增益覆盖了自动计算值（自动值本应为 %+.1f dB）。" % auto)
        return plan

    def _refresh(self) -> None:
        plan = self._current_plan()
        if plan is None:
            text = "请选择源文件与输出文件后自动生成指令。"
            notes = []
            if hasattr(self, "lbl_gain_why"):
                self.lbl_gain_why.configure(text="")
        else:
            text = core.command_string(plan)
            if self.var_gainmode.get() != "manual":
                # keep the box showing the live computed value, so switching tracks
                # visibly updates it instead of leaving the previous number behind
                self.var_gain.set("%.1f" % plan.gain_db)
            # one-line justification right next to the gain box
            peak = None
            tp = getattr(self, "_truepeak", None)
            if tp is not None and tp.ok and tp.peak_db is not None:
                peak = max(tp.peak_db, self.signal.peak_db) if (self.signal and self.signal.peak_db is not None) else tp.peak_db
            elif self.signal and self.signal.ok:
                peak = self.signal.peak_db
            if hasattr(self, "lbl_gain_why"):
                if peak is None:
                    self.lbl_gain_why.configure(
                        text="（尚未测量：此为假定的 %.1f dB，请点“测量源文件”）" % plan.gain_db,
                        foreground="#c60")
                else:
                    self.lbl_gain_why.configure(
                        text="= 目标 %.1f dBFS − 实测峰值 %.2f dBFS" % (plan.target_peak_dbfs, peak),
                        foreground="#666")
            notes = list(plan.notes) + [("⚠ " + w) for w in plan.warnings]
            notes.append("")
            notes.append("说明：0 dBDSD = 50%% 调制 = PCM −6 dBFS；SACD 峰值上限 +3.1 dBDSD ≈ PCM −2.9 dBFS。"
                         "增益由源文件实测峰值计算，全程只衰减不放大（除非源本身留有余量）。")
            notes.append("提示：现代母带普遍峰值归一化到 0 dBFS，这类文件算出的增益都会是 −6.0 dB —— "
                         "这是真实峰值代入公式的结果，不是固定值；换一首有真实余量的录音（如古典）就会变。")
            notes.append("指令中全局选项在输入文件之前、效果在输出文件之后，顺序不可调换。")
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
    root.minsize(940, 760)
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
