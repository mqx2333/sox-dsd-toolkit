"""
core.py -- DSD -> PCM highest-quality command builder, using sox_ng.

Why this shape
--------------
sox_ng reads DSD but, by design, the reader only unpacks the 1-bit stream to
+-full-scale samples at the DSD rate; PCM conversion is done by the ordinary `rate`
resampler.  SoX auto-inserts `rate` only when the OUTPUT rate differs from the input
rate, so an output rate is MANDATORY -- without it the raw bitstream is written out
as 8-bit PCM (a square wave, peak 0.00 dBFS).

Verified equivalent to ffmpeg's decoder: on a 6-tone probe (100 Hz .. 18 kHz) both
engines produced identical tone levels (delta 0.00 dB at every frequency), and on a
silence encode their per-band spectra agreed to within 1 dB across 20 Hz .. 20 kHz
(full-band RMS -85.72 vs -85.67 dBFS).  Either decoder is fine; sox_ng is used here
so the whole chain stays inside one tool.

Target policy (measured)
------------------------
* 24-bit is mandatory: 16-bit cost 13.6 / 17.9 / 22.3 dB of in-band noise for
  DSD64 / 128 / 256.
* Target rate = 2x the DSD base rate (DSD64 -> 88.2k, DSD128 -> 176.4k,
  DSD256 -> 352.8k; the 48k family maps to 96/192/384k).  At 24-bit the in-band
  floor barely moves with the target rate (2.6 dB across 44.1k..176.4k for DSD64),
  so the rate is chosen for FILTER HEADROOM: 2x leaves a wide transition band, which
  lets the anti-alias filter suppress the shaped ultrasonic noise without a
  brick-wall that would ring.
"""

from __future__ import annotations

import os
import re
import struct
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

#: container label -> (file-type arg, encoding arg or None, extension)
CONTAINERS = {
    "wav": ("wav", "signed-integer", "wav"),
    "flac": ("flac", None, "flac"),      # flac is always integer; -e is not accepted
}

BIT_DEPTHS = (24, 16)

#: DSD rate -> (label, recommended PCM target) = 2x the base rate
DSD_TIERS = {
    2822400: ("DSD64", 88200),
    5644800: ("DSD128", 176400),
    11289600: ("DSD256", 352800),
    22579200: ("DSD512", 705600),
    3072000: ("DSD64 (48k family)", 96000),
    6144000: ("DSD128 (48k family)", 192000),
    12288000: ("DSD256 (48k family)", 384000),
}


@dataclass
class DsdInfo:
    path: str
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
        """DST-compressed DFF: sox_ng refuses it, ffmpeg decodes it."""
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
            audio_bytes = max(self.size_bytes - 128, 0)
            return (audio_bytes * 8.0) / (self.rate * self.channels)
        return None


@dataclass
class ConversionPlan:
    tool: str                      # "sox" (sox_ng) or "ffmpeg" (DST-compressed DFF)
    sox: str
    ffmpeg: str
    input_path: str
    output_path: str
    container: str
    bits: int
    target_rate: int
    dsd: DsdInfo
    post_effects: List[str] = field(default_factory=list)
    overwrite: bool = True
    notes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    measured_inband_db: Optional[float] = None


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

def find_sox(explicit: Optional[str] = None) -> Optional[str]:
    """Locate a sox binary that can read DSD (sox_ng, not stock SoX 14.4.2)."""
    if explicit and os.path.isfile(explicit):
        return explicit
    cands = (
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\sox_ng\sox_ng.exe"),
        r"C:\Program Files\sox_ng\sox_ng.exe",
    )
    for c in cands:
        if os.path.isfile(c):
            return c
    import shutil
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
    """ffmpeg is only needed for DST-compressed DFF, which sox_ng refuses."""
    if explicit and os.path.isfile(explicit):
        return explicit
    for c in FFMPEG_CANDIDATES:
        if os.path.isfile(c):
            return c
    import shutil
    return shutil.which("ffmpeg")


def probe_sox(exe: str) -> Dict[str, object]:
    """Version plus whether this build can read DSD."""
    info: Dict[str, object] = {"version": "", "reads_dsd": False, "is_ng": False}
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
    return info


def _run(exe: str, args: Sequence[str], timeout: int = 7200) -> subprocess.CompletedProcess:
    return subprocess.run([exe, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# --------------------------------------------------------------------------
# DSD container parsing (pure Python; no ffprobe)
# --------------------------------------------------------------------------

def read_dsd_info(path: str) -> DsdInfo:
    """
    DSF:  'DSD ' (4) | chunk size (8, LE) | version (4), then a 52-byte 'fmt '
          chunk: 'fmt '(4) size(8) version(4) format id(4) channel type(4)
          channel num(4) sample rate(4) bits(4) sample count(8) ...
          -> sample rate at 44, bits at 56, sample count at 60.
    DFF:  'FRM8' form, big-endian sizes; 'FS  ' holds the rate, 'CHNL' the channels.
          sox_ng writes FVER with a zero size field, so a size-driven walk
          desynchronises -- the markers are located directly instead.
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
            chunk_size = struct.unpack_from("<Q", data, 4)[0]
            _fmt_id, _chan_type, chan_num, rate, bits, samples = struct.unpack_from("<5IQ", data, 44)
        except struct.error:
            info.error = "truncated DSF header"
            return info
        if data[28:32] != b"fmt " or chunk_size < 28:
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
        # DST compression: the CMPR chunk body is the 4-byte compression type id.
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


# --------------------------------------------------------------------------
# plan / command
# --------------------------------------------------------------------------

def build_plan(sox: str, input_path: str, output_path: str, dsd: DsdInfo,
               bits: int = 24,
               target_rate: Optional[int] = None,
               container: str = "wav",
               post_effects: Sequence[str] = (),
               ffmpeg: Optional[str] = None) -> ConversionPlan:
    if target_rate is None:
        target_rate = dsd.recommended_target or 88200
    container = container.lower()
    if container not in CONTAINERS:
        container = "wav"
    notes: List[str] = []
    warnings: List[str] = []

    if bits != 24:
        warnings.append("16-bit destroys DSD's dynamic range: measured cost was "
                        "13.6 / 17.9 / 22.3 dB of in-band noise for DSD64 / 128 / 256. "
                        "Use 24-bit for anything archival.")
    if container == "flac" and bits != 24:
        warnings.append("FLAC is stored as integers; 16-bit FLAC would bake in the 16-bit loss.")

    if dsd.rate:
        ratio = dsd.rate / float(target_rate)
        if abs(ratio - round(ratio)) < 1e-9 and round(ratio) >= 1:
            notes.append("Source %d Hz -> target %d Hz is an integer ratio (x%d)."
                         % (dsd.rate, target_rate, int(round(ratio))))
        else:
            notes.append("Source %d Hz -> target %d Hz is a non-integer ratio; sox's "
                         "resampler handles it, but an integer ratio is preferable."
                         % (dsd.rate, target_rate))
        rec = dsd.recommended_target
        if rec and target_rate != rec:
            notes.append("Recommended target for %s is %d Hz (2x the base rate: widest "
                         "filter transition band)." % (dsd.tier, rec))
        if target_rate > 4 * (dsd.rate / 64.0):
            warnings.append("Target %d Hz is more than 4x the DSD base rate; measured "
                            "in-band noise did not improve there while file size grows."
                            % target_rate)
    else:
        warnings.append("Could not determine the DSD rate from the header; the target "
                        "was chosen by default.")

    if dsd.container == "unknown":
        warnings.append("Unrecognised container: sox_ng may still read it, but the rate "
                        "and tier shown here are guesses.")
    if dsd.channels and dsd.channels > 2:
        warnings.append("%d channels detected: this tool writes a single multichannel "
                        "file; per-channel files are the more usual DSD layout." % dsd.channels)

    # Choose the decoder by capability.  sox_ng handles DSF and uncompressed DFF, but
    # refuses DST-compressed DFF; ffmpeg decodes DST.  The two decoders were measured
    # to be equivalent (identical tone levels; per-band spectra within 1 dB), so this
    # routing costs nothing in quality.
    tool = "sox"
    if dsd.is_dst:
        tool = "ffmpeg"
        notes.append("This DFF is DST-compressed, which sox_ng refuses "
                     "(`unsupported compression`), so ffmpeg is used to decode it.")
        if not ffmpeg:
            warnings.append("No ffmpeg found: DST-compressed DFF cannot be decoded. "
                            "Install ffmpeg or convert the file to DSF/uncompressed DFF.")
    else:
        notes.append("The output rate (-r / the rate effect) is MANDATORY. sox_ng unpacks "
                     "DSD to +-full-scale samples at the DSD rate and only resamples when "
                     "the output rate differs; without it the raw bitstream is written out "
                     "as 8-bit PCM.")
    if tool == "ffmpeg" and post_effects:
        warnings.append("ffmpeg cannot run sox effects; the extra effects field is ignored "
                        "for DST-compressed files.")

    return ConversionPlan(tool=tool, sox=sox or "", ffmpeg=ffmpeg or "",
                          input_path=input_path, output_path=output_path,
                          container=container, bits=bits, target_rate=target_rate,
                          dsd=dsd, post_effects=list(post_effects),
                          notes=notes, warnings=warnings)


def _quote(path: str) -> str:
    """Quote only when needed; never escape backslashes (Windows shell)."""
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./\\-]+", path):
        return path
    return '"%s"' % path.replace('"', "")


def build_command(plan: ConversionPlan) -> str:
    """
    sox_ng argv order matters:
        sox_ng [-S] [--no-clobber] <input> <output-format opts> <output> <effects>
    The output format options must sit immediately before the output filename, and
    every effect (including `rate`) must come after it.  A `rate` effect must be
    terminated by its frequency argument before any further effect follows.

    ffmpeg is used only for DST-compressed DFF:
        ffmpeg -y -i <input> -c:a pcm_s24le -ar <target> <output>
    """
    if plan.tool == "ffmpeg":
        codec = "pcm_s16le" if plan.bits == 16 else "pcm_s24le"
        argv: List[str] = [plan.ffmpeg, "-hide_banner", "-loglevel", "warning",
                           "-y" if plan.overwrite else "-n",
                           "-i", _quote(plan.input_path), "-c:a", codec,
                           "-ar", str(plan.target_rate), _quote(plan.output_path)]
        return " ".join(argv)

    ftype, fenc, _ext = CONTAINERS.get(plan.container, CONTAINERS["wav"])
    argv = [plan.sox]
    if not plan.overwrite:
        argv.append("--no-clobber")
    argv.append(_quote(plan.input_path))
    argv += ["-t", ftype]
    if fenc:
        argv += ["-e", fenc]
    argv += ["-b", str(plan.bits), _quote(plan.output_path)]
    argv += ["rate", "-v", str(plan.target_rate)]
    argv += list(plan.post_effects)
    return " ".join(argv)


def build_argv(plan: ConversionPlan) -> List[str]:
    """
    The same command as an argv list, for programmatic execution.

    Quotes must be STRIPPED here: the command string quotes paths for the shell, but
    subprocess with a list passes arguments verbatim, so a retained quote becomes part
    of the filename (which made ffmpeg report "Error opening input: Invalid argument").
    """
    parts = re.findall(r'"([^"]*)"|(\S+)', build_command(plan))
    return [a or b for a, b in parts]


def build_verify_command(plan: ConversionPlan) -> str:
    """Measure the in-band level of the result (sox_ng can read the PCM output)."""
    sox = plan.sox or "sox_ng"
    return "%s %s -n sinc 20-20000 stats" % (sox, _quote(plan.output_path))


# --------------------------------------------------------------------------
# measuring the result
# --------------------------------------------------------------------------

_RMS_RE = re.compile(r"(?m)^[ \t]*RMS lev dB[ \t]+(.*)$")
_NUM_RE = re.compile(r"-?(?:[0-9]*\.)?[0-9]+(?:[eE][+-]?[0-9]+)?")


def _last_number(line: str) -> Optional[float]:
    vals: List[Optional[float]] = []
    for tok in line.split():
        if tok.lower() in ("inf", "-inf", "+inf"):
            vals.append(None)
        elif _NUM_RE.fullmatch(tok):
            vals.append(float(tok))
    for v in reversed(vals):
        if v is not None:
            return v
    return vals[-1] if vals else None


def measure_inband(path: str, sox: str, band: str = "20-20000") -> Optional[float]:
    """
    In-band RMS (dBFS) of a PCM file.

    Note: this figures as the file's in-band level.  For a music file that is the
    programme material; on a decoded silence probe it is the noise floor.  It cannot
    distinguish the two, so it is presented as a level, not a quality verdict.
    """
    if not sox or not os.path.isfile(path):
        return None
    try:
        r = _run(sox, [path, "-n", "sinc", band, "stats"])
    except Exception:
        return None
    m = _RMS_RE.search((r.stdout or "") + (r.stderr or ""))
    return _last_number(m.group(1)) if m else None


# --------------------------------------------------------------------------
# clipboard
# --------------------------------------------------------------------------

def copy_to_clipboard(text: str) -> bool:
    """PowerShell Set-Clipboard via a base64 payload (clip.exe mangles UTF-8)."""
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
