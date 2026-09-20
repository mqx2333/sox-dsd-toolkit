"""
measure_dsd_noise.py -- in-band noise floor of DSD encodings and the equivalent
PCM bit depth.

Decoder: ffmpeg, NOT sox.
------------------------
sox_ng 14.8.0+git (and stock sox) WRITE DSD but cannot READ it back: reading a
.dsf yields the raw 1-bit stream reinterpreted as 8-bit PCM -- a 1.4112 MHz square
wave with exactly two sample values (+-1.0, half each) and RMS 0 dBFS.  Measuring
that produces filter artefacts, not DSD noise (it returned an identical -46.74 dB
for DSD64/128/256, and a nonsensical -373 dB for one file).  ffmpeg decodes DSD
properly, so it is used here.

Method
------
1. encode digital silence and a 997 Hz tone at the 0 dBDSD reference (-6 dBFS)
2. decode with ffmpeg to 24-bit PCM at 44.1 kHz
3. idle floor  = RMS of the decoded silence in 20 Hz - 20 kHz
   tone floor  = sqrt(total^2 - signal^2) with the signal known from the source
4. equivalent PCM depth: N = (SNR - 1.76) / 6.02
"""

import math
import os
import subprocess
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

WORK = r"C:\Harness\sox-dsd\_eq"
SOX = core.find_sox()
FFMPEG = r"C:\ffmpeg-2025-07-23-git-829680f96a-full_build\bin\ffmpeg.exe"
FS = 44100
F_TONE = 997.0
BAND = (20.0, 20000.0)

RATES = {"DSD64": (2822400, "clans-8"), "DSD128": (5644800, "clans-7"),
         "DSD256": (11289600, "clans-6")}


def sox(*args, timeout=3600):
    return core._run(SOX, list(args), timeout=timeout)


def decode_bandlimited(dsd, tag):
    """
    DSD -> 24-bit PCM at 44.1 kHz, using ffmpeg.

    ffmpeg's DSD decoder applies its own low-pass, which is what we want: the
    decimation keeps the audio band and discards the shaped ultrasonic noise.
    24-bit quantisation (-146 dBFS) sits far below any DSD floor measured here.
    """
    out = os.path.join(WORK, "ff_%s.wav" % tag)
    if os.path.exists(out):
        return out, ""
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-i", dsd, "-c:a", "pcm_s24le", "-ar", str(FS), out]
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if p.returncode != 0 or not os.path.exists(out):
        return None, (p.stderr or "")[:200]
    return out, ""


def read_wav(path):
    """
    Minimal RIFF reader that also handles IEEE float (wFormatTag == 3), which
    Python's `wave` module refuses.  Returns (mono_float64, sample_rate).
    """
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file: %s" % path)

    pos, fmt, payload = 12, None, None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = body
        elif cid == b"data":
            payload = body
        pos += 8 + size + (size & 1)

    if fmt is None or payload is None:
        raise ValueError("missing fmt/data chunk in %s" % path)

    tag = int.from_bytes(fmt[0:2], "little")
    channels = int.from_bytes(fmt[2:4], "little")
    rate = int.from_bytes(fmt[4:8], "little")
    bits = int.from_bytes(fmt[14:16], "little")

    # WAVE_FORMAT_EXTENSIBLE: the real format is the first two bytes of the
    # SubFormat GUID that follows the cbSize field.
    if tag == 0xFFFE and len(fmt) >= 26:
        tag = int.from_bytes(fmt[24:26], "little")

    if tag == 3 and bits == 32:
        x = np.frombuffer(payload, dtype="<f4").astype(np.float64)
    elif tag == 3 and bits == 64:
        x = np.frombuffer(payload, dtype="<f8").astype(np.float64)
    elif tag == 1 and bits == 32:
        x = np.frombuffer(payload, dtype="<i4").astype(np.float64) / 2 ** 31
    elif tag == 1 and bits == 24:
        a = np.frombuffer(payload, dtype=np.uint8).reshape(-1, 3)
        v = (a[:, 0].astype(np.int32) | (a[:, 1].astype(np.int32) << 8)
             | (a[:, 2].astype(np.int32) << 16))
        v = np.where(v >= 2 ** 23, v - 2 ** 24, v)
        x = v.astype(np.float64) / 2 ** 23
    elif tag == 1 and bits == 16:
        x = np.frombuffer(payload, dtype="<i2").astype(np.float64) / 2 ** 15
    else:
        raise ValueError("unsupported wav: tag=%d bits=%d" % (tag, bits))

    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    return x, rate


def rms_db(x):
    if len(x) == 0:
        return float("-inf")
    r = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    return 20 * math.log10(r) if r > 0 else float("-inf")


def a_weight_db(f):
    f = np.asarray(f, dtype=float)
    f2 = f ** 2
    ra = (12194.0 ** 2 * f2 ** 2) / (
        (f2 + 20.6 ** 2) * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2))
        * (f2 + 12194.0 ** 2))
    return 20.0 * np.log10(np.maximum(ra, 1e-30)) + 2.0


def a_weighted_rms_db(x, sr):
    """A-weighted RMS of a band-limited signal, computed by FFT bin weighting."""
    n = 1 << int(math.floor(math.log2(max(len(x), 1024))))
    if n < 1024 or len(x) < 1024:
        return float("nan")
    seg = x[-n:] if len(x) >= n else np.pad(x, (0, n - len(x)))
    win = np.hanning(n)
    cg = win.sum() / n
    spec = np.fft.rfft((seg - seg.mean()) * win)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    power = (np.abs(spec) ** 2) / (n ** 2 * cg ** 2)
    w = 10 ** (a_weight_db(freqs) / 10.0)
    tot = float((power * w).sum()) * 2.0          # one-sided
    return 10 * math.log10(tot) if tot > 0 else float("-inf")


def main():
    os.makedirs(WORK, exist_ok=True)
    sil = os.path.join(WORK, "src_sil.wav")
    ton = os.path.join(WORK, "src_ton.wav")
    sox("-n", "-r", str(FS), "-c", "2", "-b", "24", sil, "trim", "0", "4")
    sox("-n", "-r", str(FS), "-c", "2", "-b", "24", ton, "synth", "4", "sin", "%g" % F_TONE, "gain", "-6")

    xsrc, _ = read_wav(ton)
    src_rms = rms_db(xsrc)
    print("source tone: %.3f dBFS RMS (0 dBDSD reference)" % src_rms)
    print("analysis band: %.0f-%.0f Hz, unweighted and A-weighted\n" % BAND)

    header = "%-8s %13s %13s %10s %12s %12s" % (
        "DSD", "idle noise", "SNR (unwtd)", "equiv bits", "SNR (A-wtd)", "equiv(A)")
    print(header)
    print("-" * (len(header) + 4))

    for label, (rate, filt) in RATES.items():
        sf = os.path.join(WORK, "sil_%s.dsf" % label)
        tf = os.path.join(WORK, "ton_%s.dsf" % label)
        for src, dst in ((sil, sf), (ton, tf)):
            if not os.path.exists(dst):
                r = sox(src, dst, "rate", "-u", str(rate), "sdm", "-f", filt, "-t", "8", "-n", "8")
                if r.returncode != 0 or not os.path.exists(dst):
                    print("%-8s  encode failed: %s" % (label, (r.stderr or "")[:120]))
                    break

        sil_pcm, err = decode_bandlimited(sf, "sil_%s" % label) if os.path.exists(sf) else (None, "no dsf")
        ton_pcm, err2 = decode_bandlimited(tf, "ton_%s" % label) if os.path.exists(tf) else (None, "no dsf")
        if not sil_pcm or not ton_pcm:
            print("%-8s  decode failed: %s %s" % (label, err, err2))
            continue

        xs, _ = read_wav(sil_pcm)
        xt, _ = read_wav(ton_pcm)
        idle = rms_db(xs)
        total = rms_db(xt)

        # noise power = total^2 - signal^2 (signal known from the source)
        tot_lin = 10 ** (total / 20)
        sig_lin = 10 ** (src_rms / 20)
        noise_lin = math.sqrt(max(tot_lin ** 2 - sig_lin ** 2, 0.0))
        noise = 20 * math.log10(noise_lin) if noise_lin > 0 else float("-inf")

        # the decode is 6.02 dB below the PCM it was made from; reference to 0 dBDSD
        snr = src_rms - noise
        snr_a = a_weighted_rms_db(xt, FS) - noise  # approximate: uses total A-wtd level
        print("%-8s %13.2f %13.2f %10.1f %12.2f %12.1f" % (
            label, idle, snr, (snr - 1.76) / 6.02, snr_a, (snr_a - 1.76) / 6.02))

    print()
    print("idle noise = RMS of the decoded silence in band (the modulator's own floor)")
    print("SNR       = 0 dBDSD reference minus in-band noise; equiv bits = (SNR-1.76)/6.02")
    return 0


if __name__ == "__main__":
    sys.exit(main())
