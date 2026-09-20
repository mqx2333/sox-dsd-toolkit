"""
measure_dsd_to_pcm.py -- which PCM target best preserves DSD quality?

The question is not "what sample rate sounds best" but "which target lets the
anti-alias filter remove the shaped ultrasonic noise WITHOUT eating the audio band
and without folding noise back down".

Metric used: the in-band noise floor of a decoded SILENCE encode.  That isolates the
converter's own behaviour, with no test signal to confuse signal-vs-noise.  A target
that folds ultrasonic noise back into the band shows a measurably higher floor.

Matrix: each DSD rate x {same-family targets, odd target} x {16-bit, 24-bit}.
"""

import math
import os
import subprocess
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core
import measure_dsd_noise as m

WORK = m.WORK
FFMPEG = m.FFMPEG
SOX = core.find_sox()
BAND = (20.0, 20000.0)

# DSD source rate -> candidate PCM targets (44.1k family first: those are integral)
TARGETS = {
    "DSD64": [44100, 88200, 176400, 96000],
    "DSD128": [88200, 176400, 352800, 192000],
    "DSD256": [176400, 352800, 705600],
}


def decode(dsd, rate, bits, tag):
    """Always WAV so the codec choice never becomes a second variable."""
    codec = "pcm_s24le" if bits == 24 else "pcm_s16le"
    out = os.path.join(WORK, "p2p_%s_%d_%d.wav" % (tag, rate, bits))
    if os.path.exists(out):
        return out
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", dsd,
           "-c:a", codec, "-ar", str(rate), out]
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return out if (p.returncode == 0 and os.path.exists(out)) else None


def band_rms_db(path):
    """RMS in 20 Hz - 20 kHz, computed on the decimated file itself."""
    r = core._run(SOX, [path, "-n", "sinc", "%g-%g" % BAND, "stats"], timeout=3600)
    vals = core._parse_stats((r.stdout or "") + (r.stderr or ""))
    v = vals.get("rms_l")
    return v


def main():
    print("in-band (20 Hz - 20 kHz) noise floor of a decoded SILENCE encode")
    print("lower is better; a folding/insufficient filter shows up as a higher number\n")
    header = "%-8s %9s %5s %12s  %s" % ("source", "target", "bits", "floor dBFS", "note")
    print(header)
    print("-" * (len(header) + 10))

    for src_label, targets in TARGETS.items():
        dsf = os.path.join(WORK, "sil_%s.dsf" % src_label)
        if not os.path.exists(dsf):
            print("%-8s  missing %s" % (src_label, os.path.basename(dsf)))
            continue
        dsd_rate = m.RATES[src_label][0]
        for rate in targets:
            ratio = dsd_rate / rate
            note = "integer x%g" % ratio if abs(ratio - round(ratio)) < 1e-9 else "NON-integer ratio"
            if rate % 44100 == 0:
                note += ", 44.1k family"
            for bits in (24, 16):
                out = decode(dsf, rate, bits, src_label)
                if not out:
                    print("%-8s %9d %5d  decode failed" % (src_label, rate, bits))
                    continue
                try:
                    floor = band_rms_db(out)
                except Exception as exc:
                    print("%-8s %9d %5d  measure failed: %s" % (src_label, rate, bits, exc))
                    continue
                print("%-8s %9d %5d %12.2f  %s" % (src_label, rate, bits, floor, note))
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
