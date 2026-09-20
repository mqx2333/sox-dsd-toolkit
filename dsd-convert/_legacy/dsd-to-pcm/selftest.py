"""
selftest.py -- smoke test for the sox_ng based DSD -> PCM command builder.

Run:  python selftest.py
Parses the real .dsf/.dff files in this project, checks the header reader, the
recommended-target rule, the mandatory-output-rate rule, the 16-bit warning, the
argv ordering, and actually executes a conversion to prove the command works.
"""

from __future__ import annotations

import os
import subprocess
import sys

import core

WORK = r"C:\Harness\sox-dsd\_eq"
AUDIT = r"C:\Harness\sox-audit"
DSF_CASES = [("DSD64", "sil_DSD64.dsf", 88200),
             ("DSD128", "sil_DSD128.dsf", 176400),
             ("DSD256", "sil_DSD256.dsf", 352800)]

failures = []


def check(label, got, want):
    ok = got == want
    print("  [%s] %s: got %r want %r" % ("PASS" if ok else "FAIL", label, got, want))
    if not ok:
        failures.append(label)


def section(t):
    print("\n=== %s ===" % t)


def main() -> int:
    section("tool discovery and DSD capability")
    sox = core.find_sox()
    print("  sox:", sox)
    if not sox:
        failures.append("no sox found")
        return 1
    probe = core.probe_sox(sox)
    print("  version   :", probe["version"])
    print("  is sox_ng :", probe["is_ng"])
    print("  reads DSD :", probe["reads_dsd"])
    if not probe["reads_dsd"]:
        failures.append("this sox cannot read DSD (stock SoX 14.4.2 has no DSD support)")

    section("DSD header parsing (pure python, no ffprobe)")
    plans = {}
    for label, fname, expect in DSF_CASES:
        path = os.path.join(WORK, fname)
        if not os.path.exists(path):
            print("  (%s missing, skipped)" % fname)
            continue
        d = core.read_dsd_info(path)
        print("  %-8s container=%s rate=%s ch=%s bits=%s duration=%s"
              % (label, d.container, d.rate, d.channels, d.bits_per_sample,
                 ("%.1fs" % d.duration_s) if d.duration_s else "?"))
        if not d.ok:
            failures.append("%s header: %s" % (label, d.error))
            continue
        check("%s rate" % label, d.rate, expect * 32)
        check("%s recommended target" % label, d.recommended_target, expect)
        plans[label] = core.build_plan(sox, path, os.path.join(AUDIT, "st_%s.wav" % label), d)

    # Optional DFF probe directory; set DSD_SELFTEST_DFF_DIR to your own material,
    # otherwise this check is skipped.
    dff_dir = os.environ.get("DSD_SELFTEST_DFF_DIR", "")
    dffs = ([f for f in os.listdir(dff_dir) if f.lower().endswith(".dff")]
            if dff_dir and os.path.isdir(dff_dir) else [])
    if dffs:
        p = os.path.join(dff_dir, dffs[0])
        d = core.read_dsd_info(p)
        print("  DFF      %-30s rate=%s ch=%s ok=%s tier=%s"
              % (dffs[0][:30], d.rate, d.channels, d.ok, d.tier))
        check("DFF rate parsed", d.ok, True)

    section("generated commands")
    for label, plan in plans.items():
        print("  %s:" % label)
        print("    " + core.build_command(plan))

    section("mandatory output rate and argv order")
    if plans:
        plan = next(iter(plans.values()))
        cmd = core.build_command(plan)
        check("has an output rate", " -r " in cmd or "rate -v" in cmd, True)
        check("rate effect present", "rate -v %d" % plan.target_rate in cmd, True)
        argv = core.build_argv(plan)
        try:
            i_in = argv.index(core._quote(plan.input_path))
            i_out = argv.index(core._quote(plan.output_path))
            i_rate = argv.index("rate")
            check("input before output", i_in < i_out, True)
            check("rate effect after output", i_out < i_rate, True)
        except ValueError as exc:
            failures.append("argv order check: %s" % exc)

    section("16-bit must warn, 24-bit must not")
    if plans:
        plan = next(iter(plans.values()))
        p16 = core.build_plan(sox, plan.input_path, plan.output_path, plan.dsd, bits=16)
        check("16-bit warning", any("16-bit" in w for w in p16.warnings), True)
        p24 = core.build_plan(sox, plan.input_path, plan.output_path, plan.dsd, bits=24)
        check("24-bit warning absent", any("16-bit" in w for w in p24.warnings), False)

    section("post effects are appended after rate")
    if plans:
        plan = next(iter(plans.values()))
        pe = core.build_plan(sox, plan.input_path, plan.output_path, plan.dsd,
                             post_effects=["gain", "-1"])
        cmd = core.build_command(pe)
        print("    " + cmd)
        check("gain after rate", cmd.index("rate") < cmd.index("gain"), True)

    section("containers")
    if plans:
        plan = next(iter(plans.values()))
        pw = core.build_plan(sox, plan.input_path, plan.output_path, plan.dsd, container="wav")
        pf = core.build_plan(sox, plan.input_path, plan.output_path, plan.dsd, container="flac")
        check("wav uses signed-integer", "-e signed-integer" in core.build_command(pw), True)
        check("flac omits -e", "-e " not in core.build_command(pf), True)
        check("flac -t", "-t flac" in core.build_command(pf), True)

    section("execute a real conversion and verify the result")
    if plans:
        plan = next(iter(plans.values()))
        out = os.path.join(AUDIT, "st_run.wav")
        argv = [plan.sox, plan.input_path, "-t", "wav", "-e", "signed-integer",
                "-b", "24", out, "rate", "-v", str(plan.target_rate)]
        p = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        print("    exit=%d  exists=%s" % (p.returncode, os.path.exists(out)))
        if p.returncode != 0 or not os.path.exists(out):
            failures.append("conversion failed: %s" % (p.stderr or "")[:120])
        else:
            r = core._run(sox, [out, "-n", "stats"])
            txt = (r.stdout or "") + (r.stderr or "")
            peak = None
            for line in txt.splitlines():
                if line.strip().startswith("Pk lev dB"):
                    toks = [t for t in line.split() if t not in ("-",)]
                    try:
                        peak = float(toks[-1])
                    except Exception:
                        pass
            print("    decoded peak: %s dBFS" % peak)
            check("decoded peak is sane (not 0.0 = raw-bit copy)", peak is not None and peak < -6.0, True)

    section("DST routing (sox_ng cannot read DST-compressed DFF; ffmpeg can)")
    ff = core.find_ffmpeg()
    print("  ffmpeg:", ff)
    dst_dir = os.environ.get("DSD_SELFTEST_DFF_DIR", "")
    dst_files = ([f for f in os.listdir(dst_dir) if f.lower().endswith(".dff")]
                 if dst_dir and os.path.isdir(dst_dir) else [])
    if dst_files:
        p = os.path.join(dst_dir, dst_files[0])
        d = core.read_dsd_info(p)
        print("  %-30s compression=%r is_dst=%s" % (dst_files[0][:30], d.compression, d.is_dst))
        check("DST detected", d.is_dst, True)
        plan = core.build_plan(sox, p, os.path.join(AUDIT, "st_dst.wav"), d, ffmpeg=ff)
        check("DST routed to ffmpeg", plan.tool, "ffmpeg")
        check("ffmpeg command uses -ar", "-ar " in core.build_command(plan), True)
    else:
        print("  (no DFF files present, skipped)")
    if plans:
        plan = next(iter(plans.values()))
        check("non-DST routed to sox", plan.tool, "sox")

    section("quoting")
    check("plain", core._quote(r"C:\a\b.dsf"), r"C:\a\b.dsf")
    check("spaces", core._quote(r"C:\a b\c.dsf"), '"C:\\a b\\c.dsf"')

    print("\n" + ("ALL CHECKS PASSED" if not failures else "FAILURES: %s" % failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
