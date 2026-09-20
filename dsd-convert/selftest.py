"""
selftest.py -- smoke test for the merged PCM <-> DSD command builder.

Run:  python selftest.py
Checks both directions against the real toolchain, and executes one conversion in
each direction to prove the commands work.
"""

from __future__ import annotations

import os
import subprocess
import sys

import core

W = r"C:\Harness\sox-dsd\_eq"
T = r"C:\Harness\sox-audit"
MUSIC = r"C:\Users\Administrator\Desktop\Project\MusicTota"

failures = []


def check(label, got, want):
    ok = got == want
    print("  [%s] %s: got %r want %r" % ("PASS" if ok else "FAIL", label, got, want))
    if not ok:
        failures.append(label)


def section(t):
    print("\n=== %s ===" % t)


def run_peak(exe, path):
    r = core._run(exe, [path, "-n", "stats"])
    vals = core.parse_stats((r.stdout or "") + (r.stderr or ""))
    return vals.get("peak")


def main() -> int:
    section("tool discovery")
    sox = core.find_sox()
    ff = core.find_ffmpeg()
    print("  sox_ng:", sox)
    print("  ffmpeg:", ff)
    if not sox:
        failures.append("sox_ng not found")
        return 1
    probe = core.probe_sox(sox)
    print("  version:", probe["version"], " reads DSD:", probe["reads_dsd"], " sdm:", probe["has_sdm"])
    if not probe["reads_dsd"]:
        failures.append("sox cannot read DSD")
    if not probe["has_sdm"]:
        failures.append("sox has no sdm effect")

    section("direction detection")
    check("dsf -> to-pcm", core.detect_direction("a.dsf"), "to-pcm")
    check("dff -> to-pcm", core.detect_direction("a.dff"), "to-pcm")
    check("flac -> to-dsd", core.detect_direction("a.flac"), "to-dsd")
    check("wav -> to-dsd", core.detect_direction("a.wav"), "to-dsd")
    check("txt -> None", core.detect_direction("a.txt"), None)

    section("gain maths")
    check("0 dBFS -> 0 dBDSD", core.compute_gain(0.0), -6.0)
    check("-3 dBFS", core.compute_gain(-3.0), -3.0)
    check("-12 dBFS quiet master", core.compute_gain(-12.0), 6.0)
    check("safety margin", core.compute_gain(0.0, safety_margin_db=1.0), -7.0)
    check("SACD ceiling target", core.compute_gain(0.0, target_peak_dbfs=-2.9), -2.9)

    section("family / ratio")
    check("44100 family", core.classify_family(44100), "44.1k")
    check("48000 family", core.classify_family(48000), "48k")
    check("44100 divides 5644800", core.is_integer_ratio(44100, 5644800), True)
    check("48000 not divide 5644800", core.is_integer_ratio(48000, 5644800), False)

    section("DSD header parsing")
    for name, expect_rate, expect_target in (("sil_DSD64.dsf", 2822400, 88200),
                                            ("sil_DSD128.dsf", 5644800, 176400),
                                            ("sil_DSD256.dsf", 11289600, 352800)):
        p = os.path.join(W, name)
        if not os.path.exists(p):
            print("  (%s missing, skipped)" % name)
            continue
        d = core.read_dsd_info(p)
        print("  %-14s container=%s rate=%s ch=%s bits=%s dst=%s"
              % (name, d.container, d.rate, d.channels, d.bits_per_sample, d.is_dst))
        check("%s rate" % name, d.rate, expect_rate)
        check("%s target" % name, d.recommended_target, expect_target)

    if os.path.isdir(MUSIC):
        dffs = [f for f in os.listdir(MUSIC) if f.lower().endswith(".dff")]
        if dffs:
            d = core.read_dsd_info(os.path.join(MUSIC, dffs[0]))
            print("  real DFF: %-28s rate=%s compression=%r is_dst=%s"
                  % (dffs[0][:28], d.rate, d.compression, d.is_dst))
            check("DST detected", d.is_dst, True)

    section("PCM -> DSD command")
    # a dedicated probe at -3 dBFS so the computed gain is non-zero (gain is omitted
    # from the command when it rounds to 0.0)
    pcm = os.path.join(T, "st_probe_minus3.wav")
    core._run(sox, ["-n", "-r", "44100", "-c", "2", "-b", "24", pcm,
                    "synth", "1", "sin", "997", "gain", "-3"])
    hdr = core.read_pcm_header(pcm, sox)
    sig = core.measure(pcm, sox)
    print("  source: %d Hz, peak %.2f dBFS" % (hdr.get("rate") or 0, sig.peak_db))
    plan = core.plan_to_dsd(sox, pcm, os.path.join(T, "st_to_dsd.dsf"),
                            hdr.get("rate"), sig.peak_db, dsd_rate=2822400)
    print("    " + core.build_command(plan))
    check("gain computed", plan.gain_db, core.compute_gain(sig.peak_db))
    check("gain is non-zero for a -3 dBFS source", abs(plan.gain_db) > 0.05, True)
    cmd = core.build_command(plan)
    check("has -f filter", " -f " in cmd, True)
    check("has sdm", " sdm " in cmd, True)
    check("gain before rate", cmd.index("gain") < cmd.index("rate"), True)
    check("rate before sdm", cmd.index("rate") < cmd.index("sdm"), True)
    check("filter always named", bool(plan.filter_name), True)
    # and a 0 dBFS source must produce the -6 dB reference gain
    check("0 dBFS -> -6 dB", core.compute_gain(0.0), -6.0)

    section("DSD -> PCM command")
    dsf = os.path.join(W, "sil_DSD64.dsf")
    if os.path.exists(dsf):
        d = core.read_dsd_info(dsf)
        p1 = core.plan_to_pcm(sox, ff, dsf, os.path.join(T, "st_to_pcm.wav"), d)
        print("    " + core.build_command(p1))
        check("routed to sox", p1.tool, "sox")
        check("output rate present", "rate -v %d" % p1.pcm_rate in core.build_command(p1), True)
        check("24-bit default", p1.bits, 24)
        p16 = core.plan_to_pcm(sox, ff, dsf, p1.output_path, d, bits=16)
        check("16-bit warns", any("16-bit" in w for w in p16.warnings), True)
    if os.path.isdir(MUSIC):
        dffs = [f for f in os.listdir(MUSIC) if f.lower().endswith(".dff")]
        if dffs:
            p = os.path.join(MUSIC, dffs[0])
            d = core.read_dsd_info(p)
            p2 = core.plan_to_pcm(sox, ff, p, os.path.join(T, "st_dst.wav"), d)
            print("    " + core.build_command(p2)[:130])
            check("DST routed to ffmpeg", p2.tool, "ffmpeg")
            check("ffmpeg uses -ar", "-ar " in core.build_command(p2), True)

    section("quoting / argv")
    check("plain", core._quote(r"C:\a\b.dsf"), r"C:\a\b.dsf")
    check("spaces", core._quote(r"C:\a b\c.dsf"), '"C:\\a b\\c.dsf"')
    if os.path.exists(dsf):
        d = core.read_dsd_info(dsf)
        p = core.plan_to_pcm(sox, ff, r"C:\a b\c.dsf", r"C:\d e\f.wav", d)
        argv = core.build_argv(p)
        check("argv strips quotes", '"C:\\a b\\c.dsf"' not in argv, True)
        check("argv keeps raw path", r"C:\a b\c.dsf" in argv, True)

    section("execute a real conversion in each direction")
    if os.path.exists(pcm):
        out = os.path.join(T, "st_run_to_dsd.dsf")
        argv = core.build_argv(core.plan_to_dsd(sox, pcm, out, hdr.get("rate"),
                                                sig.peak_db, dsd_rate=2822400))
        p = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ok = p.returncode == 0 and os.path.exists(out)
        print("  PCM->DSD exit=%s exists=%s size=%s" % (p.returncode, os.path.exists(out),
              os.path.getsize(out) if os.path.exists(out) else 0))
        if not ok:
            failures.append("PCM->DSD conversion failed: %s" % (p.stderr or "")[:120])
    if os.path.exists(dsf):
        d = core.read_dsd_info(dsf)
        out2 = os.path.join(T, "st_run_to_pcm.wav")
        argv = core.build_argv(core.plan_to_pcm(sox, ff, dsf, out2, d, pcm_rate=88200))
        p = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        peak = run_peak(sox, out2) if os.path.exists(out2) else None
        print("  DSD->PCM exit=%s decoded peak=%s dBFS" % (p.returncode, peak))
        check("decode peak is sane (not 0.0 = raw-bit copy)", peak is not None and peak < -6.0, True)

    print("\n" + ("ALL CHECKS PASSED" if not failures else "FAILURES: %s" % failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
