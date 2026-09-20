"""
core.py -- PCM -> DSD conversion command builder for sox_ng.

Design rules (agreed with the user):
  * Always build the HIGHEST-QUALITY command, never a fast one.
  * Gain is COMPUTED, never guessed:
        DSD level convention: 0 dBDSD == 0.5 modulator full-scale == -6 dBFS PCM.
        sox's sdm scales its input by 0.5 (src/sdm.c), so a PCM peak of 1.0 drives
        the modulator to full scale, well past its stable range (~0.71 == +3.1 dBDSD,
        the SACD peak limit).  We therefore place the measured peak at the 0 dBDSD
        reference (-6 dBFS) by default.
  * Resampling is decided, not hardcoded:
        - source rate already an integer divisor of the DSD target -> no rate effect
        - source is 44.1k family (same family as 2822400 / 5644800 / 11289600)
          -> rate with -t OFF (integer ratio, -t buys nothing)
        - source is another family (48k etc.) -> rate with -t ON (irrational ratio)
  * The output container's supported rates constrain the choices, so we query
    `sox_ng --help-format <ext>` instead of assuming.

No third-party dependencies.  Python 3.8+.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

DEFAULT_SOX_CANDIDATES = (
    r"C:\Users\Administrator\AppData\Local\Programs\sox_ng\sox_ng.exe",
    r"C:\Program Files\sox_ng\sox_ng.exe",
    r"C:\Program Files (x86)\sox_ng\sox_ng.exe",
)

#: DSD reference: 0 dBDSD (0.5 modulator full-scale) == this many dBFS
DEFAULT_TARGET_PEAK_DBFS = -6.0

#: filters offered by sox's sdm effect, and the rate tier each one is designed for
FILTERS = tuple("clans-%d" % n for n in range(4, 9)) + tuple("sdm-%d" % n for n in range(4, 9))

FILTER_RATE_TIERS = {
    2822400: ("sdm-4", "sdm-5", "sdm-6", "sdm-7", "sdm-8"),
    5644800: ("sdm-6", "sdm-7"),
    11289600: ("sdm-6",),
}

#: what we consider "the 44.1 kHz family"
FAMILY_441 = (11025, 22050, 44100, 88200, 176400, 352800, 705600, 1411200)
FAMILY_48 = (8000, 16000, 24000, 32000, 48000, 96000, 192000, 384000, 768000)

_DSD_TARGET_RE = re.compile(r"([0-9.]+)e\+0?([0-9]+)")
# sox prints one column per channel plus an "Overall" column, so the number of
# values varies (mono -> 1, stereo -> 3).  Capture the whole line and take the first.
_PEAK_RE = re.compile(r"(?m)^[ \t]*Pk lev dB[ \t]+(.*)$")
_RMS_RE = re.compile(r"(?m)^[ \t]*RMS lev dB[ \t]+(.*)$")
_DC_RE = re.compile(r"(?m)^[ \t]*DC offset[ \t]+(-?[0-9.eE+-]+)")
_LEN_RE = re.compile(r"(?m)^[ \t]*Length s[ \t]+([0-9.]+)")
_NUM_RE = re.compile(r"-?(?:[0-9]*\.)?[0-9]+(?:[eE][+-]?[0-9]+)?")


def _numbers(line: str) -> List[Optional[float]]:
    """All numeric fields on a stats line, with inf/-inf mapped to None."""
    vals: List[Optional[float]] = []
    for tok in line.split():
        if tok.lower() in ("inf", "-inf", "+inf"):
            vals.append(None)
        elif _NUM_RE.fullmatch(tok):
            try:
                vals.append(float(tok))
            except ValueError:
                pass
    return vals


# --------------------------------------------------------------------------
# data holders
# --------------------------------------------------------------------------

@dataclass
class SignalInfo:
    """Result of `sox_ng <file> -n stats`."""
    path: str
    peak_db: Optional[float] = None       # worst-channel sample peak, dBFS
    peak_db_left: Optional[float] = None
    peak_db_right: Optional[float] = None
    rms_db: Optional[float] = None
    dc_offset: Optional[float] = None
    length_s: Optional[float] = None
    raw: str = ""
    ok: bool = False
    error: str = ""

    @property
    def clipped_source(self) -> bool:
        """True when the source already sits at digital full scale."""
        return self.peak_db is not None and self.peak_db >= -0.001


@dataclass
class ConversionPlan:
    """Everything the GUI needs to show and to build a command from."""
    sox: str
    input_path: str
    output_path: str
    source_rate: Optional[int]
    target_rate: int
    family: str                     # '44.1k' | '48k' | 'other' | 'unknown'
    use_rate_effect: bool
    use_irrational_accuracy: bool   # i.e. -t
    gain_db: float
    filter_name: str
    trellis_order: int
    trellis_paths: int
    trellis_latency: int
    show_progress: bool
    no_clobber: bool
    extreme_rate_opts: bool
    target_peak_dbfs: float = DEFAULT_TARGET_PEAK_DBFS
    measured_peak_db: Optional[float] = None
    notes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# sox discovery / capability probing
# --------------------------------------------------------------------------

def find_sox(explicit: Optional[str] = None) -> Optional[str]:
    """Locate a sox binary.  Prefers sox_ng over stock sox."""
    if explicit and os.path.isfile(explicit):
        return explicit
    for cand in DEFAULT_SOX_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    for name in ("sox_ng", "sox_ng.exe"):
        found = shutil.which(name)
        if found:
            return found
    return shutil.which("sox")


def _run(exe: str, args: Sequence[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [exe, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def probe_sox(exe: str) -> Dict[str, object]:
    """Return {'version':..., 'has_sdm':..., 'containers':{ext: [rates]}, ...}."""
    info: Dict[str, object] = {
        "version": "", "has_sdm": False, "containers": {}, "sdm_usage": "",
    }
    try:
        ver = _run(exe, ["--version"])
        info["version"] = (ver.stdout or ver.stderr).strip().splitlines()[0] if (ver.stdout or ver.stderr) else ""
    except Exception as exc:  # pragma: no cover - defensive
        info["version"] = "unknown (%s)" % exc

    try:
        helpeff = _run(exe, ["--help-effect", "sdm"])
        text = (helpeff.stdout or "") + (helpeff.stderr or "")
        info["has_sdm"] = "sdm" in text and "filter" in text
        info["sdm_usage"] = " ".join(text.split())[:200]
    except Exception:
        pass

    # containers we may target, and the rates each one accepts
    for ext in ("dsf", "dff", "wsd"):
        try:
            r = _run(exe, ["--help-format", ext])
            text = (r.stdout or "") + (r.stderr or "")
            if not text.strip():
                continue
            if "Writes:" not in text:
                continue
            rates: List[int] = []
            for m in _DSD_TARGET_RE.finditer(text):
                mantissa, exp = m.group(1), int(m.group(2))
                rates.append(int(float(mantissa) * (10 ** exp)))
            info["containers"][ext] = sorted(set(rates))
        except Exception:
            continue
    return info


def target_rates_for(containers: Dict[str, Sequence[int]], ext: str) -> List[int]:
    """Rates the given container can be written at (fallback: the three DSD tiers)."""
    rates = list(containers.get(ext.lower(), []) or [])
    if not rates:
        rates = [2822400, 5644800, 11289600]
    return sorted(rates)


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------

def _parse_stats(text: str) -> Dict[str, Optional[float]]:
    """
    Parse a `stats` table.

    Column counts vary with channel count, and for stereo the LAST column is the
    "Overall" figure, so we always take the final value on the line.
    """
    out: Dict[str, Optional[float]] = {}

    def last_value(rx: re.Pattern) -> Optional[float]:
        m = rx.search(text)
        if not m:
            return None
        vals = _numbers(m.group(1))
        return vals[-1] if vals else None

    def ch_values(rx: re.Pattern) -> List[float]:
        m = rx.search(text)
        if not m:
            return []
        return [v for v in _numbers(m.group(1)) if v is not None]

    peak = last_value(_PEAK_RE)
    if peak is not None:
        out["peak"] = peak
    else:
        # all channels at -inf (digital silence) -> sox prints -inf
        m = _PEAK_RE.search(text)
        if m and "-inf" in m.group(1):
            out["peak"] = -999.0

    peaks = ch_values(_PEAK_RE)
    if peaks:
        out["peak_l"] = peaks[0]
        out["peak_r"] = peaks[1] if len(peaks) > 1 else peaks[0]

    rms_vals = ch_values(_RMS_RE)
    if rms_vals:
        out["rms_l"] = rms_vals[0]
        out["rms_r"] = rms_vals[1] if len(rms_vals) > 1 else rms_vals[0]

    m = _DC_RE.search(text)
    if m:
        try:
            out["dc"] = float(m.group(1))
        except ValueError:
            pass
    m = _LEN_RE.search(text)
    if m:
        try:
            out["length"] = float(m.group(1))
        except ValueError:
            pass
    return out


def measure_all(path: str, sox: str, truepeak: bool = False):
    """
    One logical pass for everything: file header (rate/channels/precision),
    peak/RMS/DC stats, and optionally a 4x-oversampled true-peak pass.

    `--i` gives the header (stdout); `stats` gives the levels (stderr).
    Returns (header, stats, truepeak_stats_or_None).
    """
    empty_header = {"rate": None, "channels": None, "precision": None, "duration": None}
    if not os.path.isfile(path):
        return (empty_header, SignalInfo(path=path, error="file not found"), None)

    header = read_header(path, sox)

    try:
        proc = _run(sox, [path, "-n", "stats"], timeout=1800)
    except Exception as exc:
        return (header, SignalInfo(path=path, error="sox failed to run: %s" % exc), None)

    text = (proc.stdout or "") + (proc.stderr or "")
    res = SignalInfo(path=path, raw=text)
    vals = _parse_stats(text)
    if "peak" in vals:
        res.peak_db = vals.get("peak")
        res.peak_db_left = vals.get("peak_l")
        res.peak_db_right = vals.get("peak_r")
        rms_l, rms_r = vals.get("rms_l"), vals.get("rms_r")
        if rms_l is not None and rms_r is not None:
            res.rms_db = max(rms_l, rms_r)
        res.dc_offset = vals.get("dc")
        res.length_s = vals.get("length")
        res.ok = True
    else:
        res.error = "could not parse stats output (sox exit %d)" % proc.returncode

    tp_info = None
    rate = header.get("rate")
    if truepeak and isinstance(rate, int) and rate > 0:
        try:
            tp_proc = _run(sox, [path, "-n", "rate", "-u", str(rate * 4), "stats"], timeout=1800)
            tp_text = (tp_proc.stdout or "") + (tp_proc.stderr or "")
            tp_vals = _parse_stats(tp_text)
            if "peak" in tp_vals:
                tp_info = SignalInfo(path=path, raw=tp_text, ok=True)
                tp_info.peak_db = tp_vals.get("peak")
                tp_info.peak_db_left = tp_vals.get("peak_l")
                tp_info.peak_db_right = tp_vals.get("peak_r")
        except Exception:
            tp_info = None

    return header, res, tp_info


def measure(path: str, sox: str, extra_effects: Sequence[str] = ()) -> SignalInfo:
    """Run `sox <path> -n <extra> stats` and parse the figures we need."""
    res = SignalInfo(path=path)
    if not os.path.isfile(path):
        res.error = "file not found"
        return res
    args = [path, "-n", *extra_effects, "stats"]
    try:
        proc = _run(sox, args, timeout=1800)
    except Exception as exc:
        res.error = "sox failed to run: %s" % exc
        return res

    text = (proc.stdout or "") + (proc.stderr or "")
    res.raw = text
    if proc.returncode != 0:
        res.error = "sox exited %d" % proc.returncode
        return res
    # stats prints on stderr; a non-zero exit with no table means a real failure
    table = text
    vals = _parse_stats(table)
    if "peak" not in vals:
        res.error = "could not parse stats output"
        return res
    res.peak_db = vals.get("peak")
    res.peak_db_left = vals.get("peak_l")
    res.peak_db_right = vals.get("peak_r")
    rms_l, rms_r = vals.get("rms_l"), vals.get("rms_r")
    if rms_l is not None and rms_r is not None:
        res.rms_db = max(rms_l, rms_r)
    res.dc_offset = vals.get("dc")
    res.length_s = vals.get("length")
    res.ok = True
    return res


def parse_input_banner(text: str) -> Dict[str, object]:
    """
    Parse the file banner sox prints before processing:

        Input File     : 'x.flac'
        Channels       : 2
        Sample Rate    : 44100
        Precision      : 24-bit
        Duration       : 00:05:50.93 = ...

    Reading the banner means we learn the real sample rate from the same call that
    measures the peak, instead of depending on `sox --i` (absent in some builds).
    """
    out: Dict[str, object] = {"rate": None, "channels": None, "precision": None, "duration": None}
    m = re.search(r"(?mi)^\s*Sample Rate\s*:\s*([0-9.]+)\s*([kK]?)", text)
    if m:
        try:
            val = float(m.group(1)) * (1000.0 if m.group(2) else 1.0)
            out["rate"] = int(round(val))
        except ValueError:
            pass
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


def read_header(path: str, sox: str) -> Dict[str, object]:
    """
    Rate/channels/precision for a file.

    `sox --i <file>` prints the file banner and exits without processing.  We must
    not rely on the banner that appears during processing: `sox <file> -n stats`
    prints only the stats table, with no banner at all.
    """
    try:
        proc = _run(sox, ["--i", path])
        hdr = parse_input_banner((proc.stdout or "") + (proc.stderr or ""))
        if hdr.get("rate"):
            return hdr
    except Exception:
        pass
    return {"rate": None, "channels": None, "precision": None, "duration": None}


# --------------------------------------------------------------------------
# the gain calculation
# --------------------------------------------------------------------------

def compute_gain(peak_dbfs: Optional[float],
                 target_peak_dbfs: float = DEFAULT_TARGET_PEAK_DBFS,
                 safety_margin_db: float = 0.0) -> float:
    """
    Gain (dB) to apply so that the measured peak lands on the DSD reference level.

        gain = target_peak_dbfs - measured_peak_dbfs

    Always <= 0 for real material: we attenuate, never boost, because boosting a
    peak-normalised master past the modulator's stable range is what causes DSD
    overload (audible as a raised in-band noise floor and idle tones).

    Examples
    --------
    >>> compute_gain(0.0)          # typical loud master
    -6.0
    >>> compute_gain(-3.0)         # already has some headroom
    -3.0
    >>> compute_gain(-12.0)        # quiet master: +6 dB is allowed here
    6.0
    """
    if peak_dbfs is None:
        return 0.0
    if peak_dbfs <= -300.0:          # digital silence
        return 0.0
    gain = (target_peak_dbfs - peak_dbfs) - safety_margin_db
    if gain >= 0:
        # Boosting is only safe on tracks that genuinely lack headroom; round to
        # 0.1 dB so the printed command stays readable.
        gain = min(gain, 120.0)
    return round(gain, 1)


def classify_family(rate: Optional[int]) -> str:
    if rate is None:
        return "unknown"
    if rate in FAMILY_441 or (rate % 44100 == 0) or (rate % 11025 == 0):
        return "44.1k"
    if rate in FAMILY_48 or (rate % 48000 == 0) or (rate % 8000 == 0):
        return "48k"
    return "other"


def is_integer_ratio(source_rate: Optional[int], target_rate: int) -> bool:
    if not source_rate or source_rate <= 0:
        return False
    return target_rate % source_rate == 0


# --------------------------------------------------------------------------
# command construction
# --------------------------------------------------------------------------

def _quote(path: str) -> str:
    """
    Quote a path only when it needs it, so readable commands stay readable.

    Backslashes are NOT escaped: the target is a Windows shell (cmd.exe/PowerShell),
    where "C:\\dir\\file.flac" would become a literal double backslash and fail to open.
    """
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./\\-]+", path):
        return path
    return '"%s"' % path.replace('"', "")


def build_plan(sox: str,
               input_path: str,
               output_path: str,
               source_rate: Optional[int],
               target_rate: int,
               target_peak_dbfs: float = DEFAULT_TARGET_PEAK_DBFS,
               safety_margin_db: float = 0.0,
               measured_peak_db: Optional[float] = None,
               filter_name: str = "sdm-6",
               trellis_order: int = 8,
               trellis_paths: int = 16,
               trellis_latency: int = 512,
               force_rate_effect: Optional[bool] = None,
               extreme_rate_opts: bool = True,
               show_progress: bool = True,
               no_clobber: bool = True) -> ConversionPlan:
    """Assemble a ConversionPlan, deciding resampling and gain from the measurements."""
    family = classify_family(source_rate)
    integer_ratio = is_integer_ratio(source_rate, target_rate)

    if force_rate_effect is None:
        # Same rate as the target -> the rate effect would be an exact no-op, so skip it.
        # Any other integer ratio is harmless at ultra quality and keeps the command explicit.
        use_rate = not (source_rate == target_rate)
    else:
        use_rate = bool(force_rate_effect)

    # -t only earns its keep on irrational ratios (48k family -> 44.1k family DSD).
    use_irrational = use_rate and not integer_ratio

    gain_db = compute_gain(measured_peak_db, target_peak_dbfs, safety_margin_db)

    notes: List[str] = []
    warnings: List[str] = []

    if source_rate is None:
        warnings.append("Could not read the source sample rate; resampling assumed to be required.")
    elif family == "44.1k":
        notes.append("Source is 44.1k family (%d Hz) and the target %d Hz is an integer multiple "
                     "(x%d): no irrational-ratio resampling needed." % (source_rate, target_rate, target_rate // source_rate))
    elif family == "48k":
        notes.append("Source is 48k family (%d Hz) but the 44.1k-family DSD target %d Hz is NOT an "
                     "integer multiple: the rate effect runs at an irrational ratio, so -t is enabled."
                     % (source_rate, target_rate))
        warnings.append("48k-family source: consider a 48k-family DSD target (e.g. %d Hz for DSD128) "
                        "to keep the ratio integral." % (48000 * 128))
    else:
        notes.append("Source rate %d Hz is neither 44.1k nor 48k family; resampling is required." % source_rate)

    if measured_peak_db is None:
        warnings.append("Peak level unknown: gain set to %.1f dB by assumption, NOT by measurement." % gain_db)
    else:
        if measured_peak_db >= -0.001:
            warnings.append("Source peak is at %.2f dBFS (digital full scale): the master may already be "
                            "clipped. The computed gain protects the modulator but cannot undo source clipping."
                            % measured_peak_db)
        notes.append("Measured peak %.2f dBFS -> gain %.1f dB places it at %.1f dBFS (0 dBDSD reference)."
                     % (measured_peak_db, gain_db, measured_peak_db + gain_db))

    if filter_name in FILTER_RATE_TIERS and filter_name not in FILTER_RATE_TIERS[target_rate]:
        warnings.append("%s is not one of the filters designed for %d Hz in sox's table (%s); "
                        "the coefficients were tuned for another rate tier."
                        % (filter_name, target_rate, ", ".join(FILTER_RATE_TIERS[target_rate])))

    return ConversionPlan(
        sox=sox,
        input_path=input_path,
        output_path=output_path,
        source_rate=source_rate,
        target_rate=target_rate,
        family=family,
        use_rate_effect=use_rate,
        use_irrational_accuracy=use_irrational,
        gain_db=gain_db,
        filter_name=filter_name,
        trellis_order=trellis_order,
        trellis_paths=trellis_paths,
        trellis_latency=trellis_latency,
        show_progress=show_progress,
        no_clobber=no_clobber,
        extreme_rate_opts=extreme_rate_opts,
        target_peak_dbfs=target_peak_dbfs,
        measured_peak_db=measured_peak_db,
        notes=notes,
        warnings=warnings,
    )


def build_command(plan: ConversionPlan) -> List[str]:
    """
    Build the argv list.  Global options MUST precede the input file; effects follow
    the output file.  Getting this order wrong is the most common sox mistake.
    """
    argv: List[str] = [plan.sox]
    if plan.show_progress:
        argv.append("-S")
    if plan.no_clobber:
        argv.append("--no-clobber")

    argv.append(_quote(plan.input_path))
    argv.append(_quote(plan.output_path))

    # Effects run in the order written.  gain comes first so that any gain-induced
    # overshoot is band-limited by the resampler instead of clipping inside it.
    if abs(plan.gain_db) >= 0.05:
        argv.append("gain")
        argv.append(("%+g" % plan.gain_db) if plan.gain_db < 0 else ("+%g" % plan.gain_db))

    if plan.use_rate_effect:
        argv.append("rate")
        argv.append("-u")                       # ultra: 32-bit-grade resampler
        if plan.use_irrational_accuracy:
            argv.append("-t")                   # only meaningful for irrational ratios
        if plan.extreme_rate_opts:
            argv.append("-b")
            argv.append("99")                   # widest pass-band the effect allows
            argv.append("-d")
            argv.append("33")                   # highest bit-accuracy the effect allows
        argv.append(str(plan.target_rate))

    argv.append("sdm")
    argv.append("-f")
    argv.append(plan.filter_name)
    argv.append("-t")
    argv.append(str(plan.trellis_order))
    argv.append("-n")
    argv.append(str(plan.trellis_paths))
    argv.append("-l")
    argv.append(str(plan.trellis_latency))

    return argv


def command_string(plan: ConversionPlan) -> str:
    """Windows-ready single line, ready to paste into PowerShell."""
    return " ".join(build_command(plan))


def copy_to_clipboard(text: str) -> bool:
    """
    Put text on the Windows clipboard without mangling non-ASCII characters.

    `clip.exe` interprets its stdin using the process ANSI code page, so a UTF-8
    pipe turns Chinese paths into mojibake.  Instead we hand PowerShell a base64
    (pure ASCII, therefore encoding-proof) payload and let .NET decode it into a
    UTF-16 string before calling Set-Clipboard.
    """
    import base64

    try:
        payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    except Exception:
        return False

    ps = (
        "$ErrorActionPreference='Stop';"
        "$b=[Convert]::FromBase64String('%s');"
        "$s=[Text.Encoding]::UTF8.GetString($b);"
        "Set-Clipboard -Value $s;"
        "if ((Get-Clipboard -Raw) -ne $s) { exit 3 }" % payload
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-OutputFormat", "Text", "-Command", ps],
            capture_output=True, text=True, errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode == 0:
            return True
    except Exception:
        pass

    # last resort: clip.exe (fine for pure-ASCII commands)
    if text.isascii():
        try:
            proc = subprocess.run(["clip"], input=text, text=True, encoding="ascii",
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return proc.returncode == 0
        except Exception:
            pass
    return False
