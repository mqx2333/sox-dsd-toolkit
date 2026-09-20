"""
core.py -- unified PCM <-> DSD command builder.

One module, two directions, auto-detected from the file extension:

    PCM (.flac/.wav/.aiff/...) --to-dsd-->  DSD (.dsf/.dff)
    DSD (.dsf/.dff)            --to-pcm-->  PCM (.wav/.flac)

Everything here is measurement-driven; the numbers behind each rule were produced by
the tests recorded in ../README.md:

PCM -> DSD
  * gain = -6.0 dBFS - measured peak, because sox's sdm scales its input by 0.5
    (src/sdm.c), so a PCM peak of 1.0 drives the modulator to full scale, well past
    its stable range (~0.71 = +3.1 dBDSD, the SACD peak limit).
  * a filter must be named: without -f, sdm_find_filter(NULL, rate) returns the
    lowest-order entry (clans-4), silently costing in-band noise.
  * trellis is off unless -t/-n/-l are given.

DSD -> PCM
  * the output rate is MANDATORY: the reader unpacks DSD to +-full-scale samples at
    the DSD rate and sox only auto-inserts `rate` when the output rate differs;
    without it the raw bitstream is written out as 8-bit PCM (peak 0.00 dBFS).
  * 24-bit is mandatory: 16-bit cost 13.6 / 17.9 / 22.3 dB of in-band noise for
    DSD64 / 128 / 256.
  * target = 2x the DSD base rate, chosen for filter headroom rather than noise
    floor (at 24-bit the target barely moves the in-band floor).
  * DST-compressed DFF: sox_ng refuses it, ffmpeg decodes it.  The two decoders were
    measured equivalent (identical tone levels, per-band spectra within 1 dB).
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

DSD_EXTENSIONS = (".dsf", ".dff", ".wsd")
PCM_EXTENSIONS = (".flac", ".wav", ".aif", ".aiff", ".aifc", ".alac", ".m4a",
                  ".ape", ".wv", ".ogg", ".mp3", ".w64", ".caf")

#: PCM -> DSD containers
TO_DSD_CONTAINERS = {"dsf": ("dsf", None, "dsf"), "dff": ("dff", None, "dff")}
#: DSD -> PCM containers: label -> (file type, encoding or None, extension)
TO_PCM_CONTAINERS = {"wav": ("wav", "signed-integer", "wav"),
                     "flac": ("flac", None, "flac")}

FILTERS = tuple("clans-%d" % n for n in range(4, 9)) + tuple("sdm-%d" % n for n in range(4, 9))

#: DSD rate -> (label, recommended PCM target = 2x base rate)
DSD_TIERS = {
    2822400: ("DSD64", 88200),
    5644800: ("DSD128", 176400),
    11289600: ("DSD256", 352800),
    22579200: ("DSD512", 705600),
    3072000: ("DSD64 (48k family)", 96000),
    6144000: ("DSD128 (48k family)", 192000),
    12288000: ("DSD256 (48k family)", 384000),
}

DEFAULT_TARGET_PEAK_DBFS = -6.0        # 0 dBDSD reference


# ---------------------------------------------------------------------------
# data holders
# ---------------------------------------------------------------------------

@dataclass
class DsdInfo:
    path: str = ""
    container: str = ""            # dsf | dff | unknown
    rate: Optional[int] = None
    channels: Optional[int] = None
    bits_per_sample: Optional[int] = None
    sample_count: Optional[int] = None
    size_bytes: int = 0
    compression: str = ""          # "" | "DST" | other
    ok: bool = False
    error: str = ""

    @property
    def is_dst(self) -> bool:
        return self.compression.upper().startswith("DST")

    @property
    def tier(self) -> str:
        if self.rate is None:
            return "unknown"
        return DSD_TIERS.get(self.rate, ("custom", None))[0]

    @property
    def recommended_target(self) -> Optional[int]:
        if self.rate is None:
            return None
        rec = DSD_TIERS.get(self.rate, (None, None))[1]
        return rec if rec else self.rate // 32

    @property
    def duration_s(self) -> Optional[float]:
        if not self.rate:
            return None
        if self.sample_count:
            return self.sample_count / float(self.rate)
        if self.size_bytes and self.channels:
            return (max(self.size_bytes - 128, 0) * 8.0) / (self.rate * self.channels)
        return None


@dataclass
class SignalInfo:
    """Result of `sox <file> -n stats`."""
    path: str = ""
    peak_db: Optional[float] = None
    peak_left: Optional[float] = None
    peak_right: Optional[float] = None
    rms_db: Optional[float] = None
    dc_offset: Optional[float] = None
    ok: bool = False
    error: str = ""

    @property
    def clipped_source(self) -> bool:
        return self.peak_db is not None and self.peak_db >= -0.001


@dataclass
class Plan:
    """A conversion plan.  Which fields matter depends on `direction`."""
    direction: str                 # "to-dsd" | "to-pcm"
    tool: str                      # "sox" | "ffmpeg"
    input_path: str
    output_path: str

    # --- PCM -> DSD ---
    gain_db: float = 0.0
    measured_peak_db: Optional[float] = None
    target_peak_dbfs: float = DEFAULT_TARGET_PEAK_DBFS
    dsd_rate: int = 2822400
    filter_name: str = "sdm-6"
    trellis_order: int = 8
    trellis_paths: int = 16
    trellis_latency: int = 512

    # --- DSD -> PCM ---
    pcm_rate: int = 88200
    bits: int = 24
    dsd: Optional[DsdInfo] = None
    post_effects: List[str] = field(default_factory=list)

    # --- shared ---
    sox: str = ""
    ffmpeg: str = ""
    overwrite: bool = True
    notes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def container(self) -> str:
        ext = os.path.splitext(self.output_path)[1].lstrip(".").lower()
        return ext or ("dsf" if self.direction == "to-dsd" else "wav")


# ---------------------------------------------------------------------------
# discovery / probing
# ---------------------------------------------------------------------------

def find_sox(explicit: Optional[str] = None) -> Optional[str]:
    """Locate a sox that can read DSD (sox_ng; stock SoX 14.4.2 cannot)."""
    if explicit and os.path.isfile(explicit):
        return explicit
    for c in (os.path.expandvars(r"%LOCALAPPDATA%\Programs\sox_ng\sox_ng.exe"),
              r"C:\Program Files\sox_ng\sox_ng.exe"):
        if os.path.isfile(c):
            return c
    for name in ("sox_ng", "sox_ng.exe"):
        found = shutil.which(name)
        if found:
            return found
    return shutil.which("sox")


FFMPEG_CANDIDATES = (
    r"C:\ffmpeg-2025-07-23-git-829680f96a-full_build\bin\ffmpeg.exe",
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
)


def find_ffmpeg(explicit: Optional[str] = None) -> Optional[str]:
    """ffmpeg is needed only for DST-compressed DFF, which sox_ng refuses."""
    if explicit and os.path.isfile(explicit):
        return explicit
    for c in FFMPEG_CANDIDATES:
        if os.path.isfile(c):
            return c
    return shutil.which("ffmpeg")


def _run(exe: str, args: Sequence[str], timeout: int = 7200) -> subprocess.CompletedProcess:
    return subprocess.run([exe, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def probe_sox(exe: str) -> Dict[str, object]:
    info: Dict[str, object] = {"version": "", "reads_dsd": False, "has_sdm": False, "is_ng": False}
    try:
        r = _run(exe, ["--version"])
        txt = (r.stdout or r.stderr or "").strip()
        info["version"] = txt.splitlines()[0] if txt else ""
        info["is_ng"] = "sox_ng" in txt.lower() or "sox_ng" in os.path.basename(exe).lower()
    except Exception as exc:
        info["version"] = "unknown (%s)" % exc
    for ext in ("dsf", "dff"):
        try:
            r = _run(exe, ["--help-format", ext])
            t = (r.stdout or "") + (r.stderr or "")
            if "Reads:" in t and "yes" in t.split("Reads:")[1].split("\n")[0]:
                info["reads_dsd"] = True
        except Exception:
            pass
    try:
        r = _run(exe, ["--help-effect", "sdm"])
        t = (r.stdout or "") + (r.stderr or "")
        info["has_sdm"] = ("sdm" in t) and ("filter" in t)
    except Exception:
        pass
    return info


def detect_direction(path: str) -> Optional[str]:
    """Infer the conversion direction from a file's extension."""
    ext = os.path.splitext(path)[1].lower()
    if ext in DSD_EXTENSIONS:
        return "to-pcm"
    if ext in PCM_EXTENSIONS:
        return "to-dsd"
    return None


# ---------------------------------------------------------------------------
# DSD container parsing (pure Python; no ffprobe)
# ---------------------------------------------------------------------------

def read_dsd_info(path: str) -> DsdInfo:
    """
    DSF:  'DSD '|size(8,LE)|version(4) then 'fmt '|size(8)|version(4)|format id(4)
          |channel type(4)|channel num(4)|sample rate(4)|bits(4)|sample count(8)
          -> channels at 52, rate at 56, bits at 60, samples at 64
          (verified byte by byte against a sox_ng-written file)
    DFF:  'FRM8' form, big-endian sizes; 'FS  ' holds the rate, 'CHNL' the channels,
          'CMPR' the compression type.  sox_ng writes FVER with a zero size field,
          so markers are located directly rather than by a size-driven walk.
    """
    info = DsdInfo(path=path)
    if not os.path.isfile(path):
        info.error = "file not found"
        return info
    try:
        info.size_bytes = os.path.getsize(path)
        with open(path, "rb") as fh:
            data = fh.read(1 << 16)
    except OSError as exc:
        info.error = "cannot read: %s" % exc
        return info

    if data[:4] == b"DSD ":
        info.container = "dsf"
        try:
            # 4-4 = 4 x uint32, 8-8 = one uint64 (sample count)
            _chan_type, chan_num, rate, bits = struct.unpack_from("<4I", data, 48)
            samples = struct.unpack_from("<Q", data, 64)[0]
        except struct.error:
            info.error = "truncated DSF header"
            return info
        if data[28:32] != b"fmt ":
            info.error = "unexpected DSF layout (`fmt ` not where expected)"
            return info
        info.channels = chan_num
        info.rate = rate
        info.bits_per_sample = bits or 1
        info.sample_count = samples or None
        if not info.rate or info.rate <= 0:
            info.error = "DSF header reports no sample rate"
            return info
        info.ok = True
        return info

    if data[:4] == b"FRM8":
        info.container = "dff"
        info.bits_per_sample = 1
        idx = data.find(b"FS  ", 12)
        if idx >= 0:
            size = int.from_bytes(data[idx + 4:idx + 12], "big")
            if size >= 4 and idx + 16 <= len(data):
                info.rate = int.from_bytes(data[idx + 12:idx + 16], "big")
        idx = data.find(b"CHNL", 12)
        if idx >= 0:
            size = int.from_bytes(data[idx + 4:idx + 12], "big")
            if size >= 2 and idx + 14 <= len(data):
                info.channels = int.from_bytes(data[idx + 12:idx + 14], "big")
        idx = data.find(b"CMPR", 12)
        if idx >= 0:
            size = int.from_bytes(data[idx + 4:idx + 12], "big")
            body = data[idx + 12:idx + 12 + max(size, 0)]
            info.compression = body[:4].decode("latin-1").strip() if body else ""
        info.ok = bool(info.rate and info.rate > 0)
        if not info.ok:
            info.error = "no usable FS chunk in DFF header"
        return info

    info.container = "unknown"
    info.error = "not a DSF or DFF header"
    return info


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------

_PEAK_RE = re.compile(r"(?m)^[ \t]*Pk lev dB[ \t]+(.*)$")
_RMS_RE = re.compile(r"(?m)^[ \t]*RMS lev dB[ \t]+(.*)$")
_DC_RE = re.compile(r"(?m)^[ \t]*DC offset[ \t]+(-?[0-9.eE+-]+)")
_NUM_RE = re.compile(r"-?(?:[0-9]*\.)?[0-9]+(?:[eE][+-]?[0-9]+)?")


def _numbers(line: str) -> List[Optional[float]]:
    vals: List[Optional[float]] = []
    for tok in line.split():
        if tok.lower() in ("inf", "-inf", "+inf"):
            vals.append(None)
        elif _NUM_RE.fullmatch(tok):
            vals.append(float(tok))
    return vals


def parse_stats(text: str) -> Dict[str, Optional[float]]:
    """
    Parse a `stats` table.  Column counts vary with channel count and for stereo the
    LAST column is the "Overall" figure, so the final value on each line is used.
    """
    out: Dict[str, Optional[float]] = {}
    m = _PEAK_RE.search(text)
    if m:
        vals = _numbers(m.group(1))
        if vals and vals[-1] is not None:
            out["peak"] = vals[-1]
        elif "-inf" in m.group(1):
            out["peak"] = -999.0
        good = [v for v in vals if v is not None]
        if good:
            out["peak_l"] = good[0]
            out["peak_r"] = good[1] if len(good) > 1 else good[0]
    m = _RMS_RE.search(text)
    if m:
        good = [v for v in _numbers(m.group(1)) if v is not None]
        if good:
            out["rms_l"] = good[0]
            out["rms_r"] = good[-1]
    m = _DC_RE.search(text)
    if m:
        out["dc"] = float(m.group(1))
    return out


def read_pcm_header(path: str, sox: str) -> Dict[str, object]:
    """Rate/channels/precision via `sox --i` (which prints a banner and exits)."""
    out: Dict[str, object] = {"rate": None, "channels": None, "precision": None, "duration": None}
    try:
        r = _run(sox, ["--i", path])
        text = (r.stdout or "") + (r.stderr or "")
    except Exception:
        return out
    m = re.search(r"(?mi)^\s*Sample Rate\s*:\s*([0-9.]+)\s*([kK]?)", text)
    if m:
        out["rate"] = int(round(float(m.group(1)) * (1000.0 if m.group(2) else 1.0)))
    m = re.search(r"(?mi)^\s*Channels\s*:\s*(\d+)", text)
    if m:
        out["channels"] = int(m.group(1))
    m = re.search(r"(?mi)^\s*Precision\s*:\s*(\d+)", text)
    if m:
        out["precision"] = int(m.group(1))
    m = re.search(r"(?mi)^\s*Duration\s*:\s*([0-9:.]+)", text)
    if m:
        out["duration"] = m.group(1)
    return out


def measure(path: str, sox: str) -> SignalInfo:
    """`sox <file> -n stats` for peak/RMS/DC."""
    info = SignalInfo(path=path)
    if not os.path.isfile(path):
        info.error = "file not found"
        return info
    try:
        r = _run(sox, [path, "-n", "stats"])
    except Exception as exc:
        info.error = "sox failed: %s" % exc
        return info
    vals = parse_stats((r.stdout or "") + (r.stderr or ""))
    if "peak" not in vals:
        info.error = "could not parse stats output"
        return info
    info.peak_db = vals.get("peak")
    info.peak_left = vals.get("peak_l")
    info.peak_right = vals.get("peak_r")
    info.rms_db = vals.get("rms_r")
    info.dc_offset = vals.get("dc")
    info.ok = True
    return info


def measure_inband(path: str, sox: str, band: str = "20-20000") -> Optional[float]:
    """In-band RMS (dBFS) of a PCM file."""
    if not sox or not os.path.isfile(path):
        return None
    try:
        r = _run(sox, [path, "-n", "sinc", band, "stats"])
    except Exception:
        return None
    vals = parse_stats((r.stdout or "") + (r.stderr or ""))
    return vals.get("rms_l")


# ---------------------------------------------------------------------------
# gain
# ---------------------------------------------------------------------------

def compute_gain(peak_dbfs: Optional[float],
                 target_peak_dbfs: float = DEFAULT_TARGET_PEAK_DBFS,
                 safety_margin_db: float = 0.0) -> float:
    """
    Gain (dB) placing the measured peak on the DSD reference level.

        gain = target_peak_dbfs - measured_peak_dbfs

    Always <= 0 for peak-normalised material: we attenuate, never boost, because
    driving a peak-normalised master into the modulator is what causes DSD overload
    (audible as a raised in-band noise floor and idle tones).

    >>> compute_gain(0.0)
    -6.0
    >>> compute_gain(-12.0)
    6.0
    """
    if peak_dbfs is None or peak_dbfs <= -300.0:
        return 0.0
    return round((target_peak_dbfs - peak_dbfs) - safety_margin_db, 1)


def classify_family(rate: Optional[int]) -> str:
    if not rate:
        return "unknown"
    if rate % 44100 == 0 or rate % 11025 == 0:
        return "44.1k"
    if rate % 48000 == 0 or rate % 8000 == 0:
        return "48k"
    return "other"


def is_integer_ratio(source_rate: Optional[int], target_rate: int) -> bool:
    return bool(source_rate) and target_rate % source_rate == 0


# ---------------------------------------------------------------------------
# plan builders
# ---------------------------------------------------------------------------

def _quote(path: str) -> str:
    """Quote only when needed; never escape backslashes (this is a Windows shell)."""
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./\\-]+", path):
        return path
    return '"%s"' % path.replace('"', "")


def plan_to_dsd(sox: str, input_path: str, output_path: str,
                source_rate: Optional[int],
                measured_peak_db: Optional[float],
                gain_mode: str = "auto",
                gain_db: float = 0.0,
                target_peak_dbfs: float = DEFAULT_TARGET_PEAK_DBFS,
                safety_margin_db: float = 0.0,
                dsd_rate: int = 2822400,
                filter_name: str = "sdm-6",
                trellis_order: int = 8,
                trellis_paths: int = 16,
                trellis_latency: int = 512,
                post_effects: Sequence[str] = ()) -> Plan:
    notes: List[str] = []
    warnings: List[str] = []
    family = classify_family(source_rate)

    if gain_mode == "manual":
        gain = round(gain_db, 1)
        auto = compute_gain(measured_peak_db, target_peak_dbfs, safety_margin_db)
        warnings.append("手动增益覆盖了自动计算值（自动值本应为 %+.1f dB）。" % auto)
    else:
        gain = compute_gain(measured_peak_db, target_peak_dbfs, safety_margin_db)

    if measured_peak_db is None:
        warnings.append("未测到峰值：增益 %.1f dB 是假定值，请先测量源文件。" % gain)
    else:
        if measured_peak_db >= -0.001:
            warnings.append("源峰值 %.2f dBFS 已顶到数字满刻度（母带可能已削波）。"
                            "增益能保护调制器，但救不回源削波。" % measured_peak_db)
        notes.append("实测峰值 %.2f dBFS -> 增益 %+.1f dB，落到 %.1f dBFS（0 dBDSD 参考）。"
                     % (measured_peak_db, gain, measured_peak_db + gain))

    if family == "44.1k" and source_rate:
        notes.append("源 %d Hz 属 44.1k 家族，到 %d Hz 是整数比（x%d）。"
                     % (source_rate, dsd_rate, max(1, dsd_rate // source_rate)))
    elif family == "48k" and source_rate:
        if not is_integer_ratio(source_rate, dsd_rate):
            notes.append("源 %d Hz 属 48k 家族，到 %d Hz 不是整数比；rate 会以非整数比工作。"
                         % (source_rate, dsd_rate))
            warnings.append("48k 家族源可考虑改选 48k 家族的 DSD 目标（如 %d Hz）以保持整数比。"
                            % (48000 * 64))
    elif source_rate:
        notes.append("源 %d Hz 既非 44.1k 也非 48k 家族，需要重采样。" % source_rate)

    if not filter_name:
        warnings.append("必须显式指定 -f：不指定时 sox 会退化成最低阶的 clans-4，带内噪声明显变差。")
    notes.append("参数要点：-f 必须显式给（否则退化成 clans-4）；trellis 默认关闭，"
                 "需给 -t/-n/-l 才启用（范围 order 3–32 / paths 4–32 / latency 100–2048）。")

    return Plan(direction="to-dsd", tool="sox", sox=sox,
                input_path=input_path, output_path=output_path,
                gain_db=gain, measured_peak_db=measured_peak_db,
                target_peak_dbfs=target_peak_dbfs,
                dsd_rate=dsd_rate, filter_name=filter_name or "sdm-6",
                trellis_order=trellis_order, trellis_paths=trellis_paths,
                trellis_latency=trellis_latency,
                post_effects=list(post_effects),
                notes=notes, warnings=warnings)


def plan_to_pcm(sox: str, ffmpeg: Optional[str], input_path: str, output_path: str,
                dsd: DsdInfo,
                bits: int = 24,
                pcm_rate: Optional[int] = None,
                post_effects: Sequence[str] = ()) -> Plan:
    notes: List[str] = []
    warnings: List[str] = []
    if pcm_rate is None:
        pcm_rate = dsd.recommended_target or 88200

    if bits != 24:
        warnings.append("16-bit 会毁掉 DSD 的动态范围：实测 DSD64/128/256 的带内噪声底分别劣化 "
                        "13.6 / 17.9 / 22.3 dB。归档请用 24-bit。")

    if dsd.rate:
        ratio = dsd.rate / float(pcm_rate)
        if abs(ratio - round(ratio)) < 1e-9 and round(ratio) >= 1:
            notes.append("源 %d Hz -> 目标 %d Hz 是整数比（x%d）。"
                         % (dsd.rate, pcm_rate, int(round(ratio))))
        else:
            notes.append("源 %d Hz -> 目标 %d Hz 非整数比；sox 的重采样器能处理，但整数比更好。"
                         % (dsd.rate, pcm_rate))
        rec = dsd.recommended_target
        if rec and pcm_rate != rec:
            notes.append("该文件的推荐目标是 %d Hz（%s 的 2 倍基率，过渡带最宽）。" % (rec, dsd.tier))
        if pcm_rate > 4 * (dsd.rate / 64.0):
            warnings.append("目标 %d Hz 超过 DSD 基率的 4 倍；实测带内噪声不再改善而体积翻倍。" % pcm_rate)
    else:
        warnings.append("未能从文件头读出 DSD 速率，目标是默认值。")

    if dsd.container == "unknown":
        warnings.append("未识别的容器：sox_ng 也许仍能读，但显示的速率/等级是猜测值。")
    if dsd.channels and dsd.channels > 2:
        warnings.append("检测到 %d 声道；本程序写单个多声道文件。" % dsd.channels)

    tool = "sox"
    if dsd.is_dst:
        tool = "ffmpeg"
        notes.append("该 DFF 是 DST 压缩，sox_ng 会拒绝（unsupported compression），改用 ffmpeg 解码。")
        if not ffmpeg:
            warnings.append("找不到 ffmpeg：DST 压缩的 DFF 无法解码。")
        if post_effects:
            warnings.append("ffmpeg 不能运行 sox 效果；DST 文件的额外效果被忽略。")
    else:
        notes.append("输出速率是必需的：sox_ng 按设计只把 1-bit 展开成 ±满幅样本、速率标为 DSD 速率，"
                     "PCM 转换交给普通 rate 重采样器；SoX 只在输出速率不同时才自动插入 rate，"
                     "不给速率就会把 1-bit 流当 8-bit PCM 原样写出。")

    return Plan(direction="to-pcm", tool=tool, sox=sox or "", ffmpeg=ffmpeg or "",
                input_path=input_path, output_path=output_path,
                pcm_rate=pcm_rate, bits=bits, dsd=dsd,
                post_effects=list(post_effects),
                notes=notes, warnings=warnings)


# ---------------------------------------------------------------------------
# command construction
# ---------------------------------------------------------------------------

def build_command(plan: Plan) -> str:
    if plan.direction == "to-dsd":
        argv = [plan.sox]
        if not plan.overwrite:
            argv.append("--no-clobber")
        argv.append(_quote(plan.input_path))
        ftype, _enc, _ext = TO_DSD_CONTAINERS.get(plan.container, TO_DSD_CONTAINERS["dsf"])
        argv += ["-t", ftype, _quote(plan.output_path)]
        if abs(plan.gain_db) >= 0.05:
            argv += ["gain", ("%+g" % plan.gain_db) if plan.gain_db < 0 else ("+%g" % plan.gain_db)]
        argv += ["rate", "-v", str(plan.dsd_rate)]
        argv += ["sdm", "-f", plan.filter_name,
                 "-t", str(plan.trellis_order),
                 "-n", str(plan.trellis_paths),
                 "-l", str(plan.trellis_latency)]
        argv += list(plan.post_effects)
        return " ".join(argv)

    # to-pcm
    if plan.tool == "ffmpeg":
        codec = "pcm_s16le" if plan.bits == 16 else "pcm_s24le"
        argv = [plan.ffmpeg, "-hide_banner", "-loglevel", "warning",
                "-y" if plan.overwrite else "-n",
                "-i", _quote(plan.input_path), "-c:a", codec,
                "-ar", str(plan.pcm_rate), _quote(plan.output_path)]
        return " ".join(argv)

    ftype, fenc, _ext = TO_PCM_CONTAINERS.get(plan.container, TO_PCM_CONTAINERS["wav"])
    argv = [plan.sox]
    if not plan.overwrite:
        argv.append("--no-clobber")
    argv.append(_quote(plan.input_path))
    argv += ["-t", ftype]
    if fenc:
        argv += ["-e", fenc]
    argv += ["-b", str(plan.bits), _quote(plan.output_path)]
    argv += ["rate", "-v", str(plan.pcm_rate)]
    argv += list(plan.post_effects)
    return " ".join(argv)


def build_argv(plan: Plan) -> List[str]:
    """argv list for programmatic execution (quotes stripped: subprocess passes verbatim)."""
    parts = re.findall(r'"([^"]*)"|(\S+)', build_command(plan))
    return [a or b for a, b in parts]


def build_verify_command(plan: Plan, sox: str) -> str:
    return "%s %s -n sinc 20-20000 stats" % (sox or "sox_ng", _quote(plan.output_path))


def suggested_output(input_path: str, direction: str, container: Optional[str] = None) -> str:
    base, ext = os.path.splitext(input_path)
    if direction == "to-dsd":
        return base + "_DSD." + (container or "dsf")
    ext = container or "wav"
    return base + "_PCM." + ext


def target_rate_choices(dsd: Optional[DsdInfo], direction: str) -> List[str]:
    if direction == "to-pcm":
        base = (dsd.rate // 32) if (dsd and dsd.rate) else None
        cands: List[int] = []
        if base:
            cands += [base // 2, base, base * 2, base * 4]
        cands += [44100, 88200, 176400, 352800, 48000, 96000, 192000, 384000]
    else:
        cands = [2822400, 5644800, 11289600, 22579200, 3072000, 6144000, 12288000]
    seen, out = set(), []
    for c in cands:
        if c and c not in seen and c >= 8000:
            seen.add(c)
            out.append(str(c))
    return out


# ---------------------------------------------------------------------------
# clipboard
# ---------------------------------------------------------------------------

def copy_to_clipboard(text: str) -> bool:
    """PowerShell Set-Clipboard via a base64 payload (clip.exe mangles non-ASCII)."""
    import base64
    try:
        payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    except Exception:
        return False
    ps = ("$ErrorActionPreference='Stop';"
          "$b=[Convert]::FromBase64String('%s');"
          "$s=[Text.Encoding]::UTF8.GetString($b);"
          "Set-Clipboard -Value $s;"
          "if ((Get-Clipboard -Raw) -ne $s) { exit 3 }" % payload)
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-OutputFormat", "Text", "-Command", ps],
                           capture_output=True, text=True, errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode == 0
    except Exception:
        return False
