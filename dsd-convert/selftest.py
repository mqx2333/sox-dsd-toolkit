"""
selftest.py -- smoke test for the PCM <-> DSD command builder.

Run:  python selftest.py

Self-contained: any probe it needs is synthesised with sox_ng into a local `_selftest`
folder, so the test runs on a fresh clone with nothing but the tools installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import core

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "_selftest")

failures = []
skipped = []


def check(label, got, want):
    ok = got == want
    print("  [%s] %s: got %r want %r" % ("PASS" if ok else "FAIL", label, got, want))
    if not ok:
        failures.append(label)


def section(t):
    print("\n=== %s ===" % t)


def make_probes(sox):
    """Synthesise everything the test needs.  Returns a dict of paths."""
    os.makedirs(WORK, exist_ok=True)
    p = {}

    # a -3 dBFS tone so the computed gain is non-zero
    p["pcm_tone"] = os.path.join(WORK, "probe_minus3.wav")
    core._run(sox, ["-n", "-r", "44100", "-c", "2", "-b", "24", p["pcm_tone"],
                    "synth", "1", "sin", "997", "gain", "-3"])

    # silence and a tone encoded to the three DSD tiers, for header + decode tests
    for label, rate, filt in (("DSD64", 2822400, "clans-8"),
                              ("DSD128", 5644800, "clans-7"),
                              ("DSD256", 11289600, "clans-6")):
        sil = os.path.join(WORK, "sil_%s.wav" % label)
        core._run(sox, ["-n", "-r", "44100", "-c", "2", "-b", "24", sil, "trim", "0", "0.5"])
        dsf = os.path.join(WORK, "sil_%s.dsf" % label)
        core._run(sox, [sil, dsf, "rate", "-u", str(rate), "sdm", "-f", filt, "-t", "8", "-n", "8"])
        p["sil_%s" % label] = dsf

    # an uncompressed DFF, to exercise the DFF reader and the sox_ng route
    dff = os.path.join(WORK, "probe.dff")
    core._run(sox, [p["pcm_tone"], dff, "rate", "-u", "2822400",
                    "sdm", "-f", "clans-8", "-t", "8", "-n", "8"])
    p["dff"] = dff
    return p


def run_peak(sox, path):
    r = core._run(sox, [path, "-n", "stats"])
    return core.parse_stats((r.stdout or "") + (r.stderr or "")).get("peak")


def main() -> int:
    section("tool discovery")
    sox = core.find_sox()
    ff = core.find_ffmpeg()
    print("  sox_ng:", sox)
    print("  ffmpeg:", ff)
    if not sox:
        print("\nFAILED: no sox found. Install sox_ng (>= 14.6.0, DSD support).")
        return 1
    probe = core.probe_sox(sox)
    print("  version  :", probe["version"])
    print("  reads DSD:", probe["reads_dsd"])
    print("  has sdm  :", probe["has_sdm"])
    if not probe["reads_dsd"]:
        failures.append("this sox cannot read DSD (stock SoX 14.4.2 cannot; use sox_ng)")
    if not probe["has_sdm"]:
        failures.append("this sox has no sdm effect (use sox_ng)")

    section("direction detection")
    check("dsf -> to-pcm", core.detect_direction("a.dsf"), "to-pcm")
    check("dff -> to-pcm", core.detect_direction("a.dff"), "to-pcm")
    check("wsd -> to-pcm", core.detect_direction("a.wsd"), "to-pcm")
    check("flac -> to-dsd", core.detect_direction("a.flac"), "to-dsd")
    check("wav -> to-dsd", core.detect_direction("a.wav"), "to-dsd")
    check("aiff -> to-dsd", core.detect_direction("a.aiff"), "to-dsd")
    check("txt -> None", core.detect_direction("a.txt"), None)

    section("gain arithmetic")
    check("0 dBFS -> 0 dBDSD", core.compute_gain(0.0), -6.0)
    check("-3 dBFS", core.compute_gain(-3.0), -3.0)
    check("-12 dBFS quiet master", core.compute_gain(-12.0), 6.0)
    check("safety margin", core.compute_gain(0.0, safety_margin_db=1.0), -7.0)
    check("SACD ceiling +3.1 dBDSD", core.compute_gain(0.0, target_peak_dbfs=-2.9), -2.9)
    check("silence is left alone", core.compute_gain(None), 0.0)

    section("sample-rate family and ratio")
    check("44100 -> 44.1k", core.classify_family(44100), "44.1k")
    check("88200 -> 44.1k", core.classify_family(88200), "44.1k")
    check("48000 -> 48k", core.classify_family(48000), "48k")
    check("96000 -> 48k", core.classify_family(96000), "48k")
    check("44100 divides 5644800", core.is_integer_ratio(44100, 5644800), True)
    check("48000 does not divide 5644800", core.is_integer_ratio(48000, 5644800), False)
    check("48000 divides 6144000", core.is_integer_ratio(48000, 6144000), True)
    check("integer ratio helper targets", core.target_rate_choices(None, "to-pcm")[:2],
          ["44100", "88200"])

    if failures:
        print("\nAborting before the tool-dependent tests: %s" % failures)
        return 1

    section("probe generation (synthesised locally, nothing is downloaded)")
    probes = make_probes(sox)
    for label, path in sorted(probes.items()):
        ok = os.path.exists(path) and os.path.getsize(path) > 0
        print("  %-12s %s  %s" % (label, "OK" if ok else "MISSING",
                                  os.path.basename(path)))
        if not ok:
            failures.append("probe %s not created" % label)
    if failures:
        print("\nAborting: could not synthesise probes.")
        return 1

    section("DSD header parsing (pure python, no ffprobe)")
    tiers = (("DSD64", 2822400, 88200), ("DSD128", 5644800, 176400),
             ("DSD256", 11289600, 352800))
    for label, rate, target in tiers:
        d = core.read_dsd_info(probes["sil_%s" % label])
        print("  %-8s container=%s rate=%s ch=%s bits=%s dst=%s target=%s"
              % (label, d.container, d.rate, d.channels, d.bits_per_sample,
                 d.is_dst, d.recommended_target))
        check("%s rate" % label, d.rate, rate)
        check("%s recommended target" % label, d.recommended_target, target)

    d = core.read_dsd_info(probes["dff"])
    print("  %-8s container=%s rate=%s ch=%s dst=%s" % ("DFF", d.container, d.rate,
                                                        d.channels, d.is_dst))
    check("DFF parses", d.ok, True)
    check("DFF container", d.container, "dff")
    check("DFF is not DST", d.is_dst, False)
    check("garbage is rejected", core.read_dsd_info(probes["pcm_tone"]).ok, False)

    section("PCM -> DSD command")
    hdr = core.read_pcm_header(probes["pcm_tone"], sox)
    sig = core.measure(probes["pcm_tone"], sox)
    print("  source: %d Hz, peak %.2f dBFS" % (hdr.get("rate") or 0, sig.peak_db))
    plan = core.plan_to_dsd(sox, probes["pcm_tone"],
                            os.path.join(WORK, "out.dsf"),
                            hdr.get("rate"), sig.peak_db, dsd_rate=2822400)
    cmd = core.build_command(plan)
    print("    " + cmd)
    check("gain computed from the measurement", plan.gain_db, core.compute_gain(sig.peak_db))
    check("gain is non-zero for a -3 dBFS source", abs(plan.gain_db) > 0.05, True)
    check("filter is always named", bool(plan.filter_name), True)
    check("command has -f", " -f " in cmd, True)
    check("command has sdm", " sdm " in cmd, True)
    check("gain comes before rate", cmd.index("gain") < cmd.index("rate"), True)
    check("rate comes before sdm", cmd.index("rate") < cmd.index("sdm"), True)
    check("trellis parameters present", all(x in cmd for x in ("-t", "-n", "-l")), True)
    check("dsf container by extension", "-t dsf" in cmd, True)

    section("DSD -> PCM command")
    dsf64 = probes["sil_DSD64"]
    d = core.read_dsd_info(dsf64)
    p1 = core.plan_to_pcm(sox, ff, dsf64, os.path.join(WORK, "out.wav"), d)
    print("    " + core.build_command(p1))
    check("routed to sox_ng", p1.tool, "sox")
    check("output rate is present", "rate -v %d" % p1.pcm_rate in core.build_command(p1), True)
    check("24-bit by default", p1.bits, 24)
    check("16-bit warns", any("16-bit" in w for w in
                              core.plan_to_pcm(sox, ff, dsf64, p1.output_path, d,
                                               bits=16).warnings), True)
    flac_plan = core.plan_to_pcm(sox, ff, dsf64, os.path.join(WORK, "out.flac"), d)
    check("flac omits -e", "-e " not in core.build_command(flac_plan), True)
    check("wav sets -e", "-e signed-integer" in core.build_command(p1), True)

    section("DST routing (sox_ng refuses DST; ffmpeg decodes it)")
    # craft a DST-flagged DFF by patching the CMPR chunk of a plain one
    dst_path = os.path.join(WORK, "fake_dst.dff")
    raw = bytearray(open(probes["dff"], "rb").read(1 << 16))
    i = raw.find(b"CMPR")
    if i >= 0:
        raw[i + 12:i + 16] = b"DST "
        with open(dst_path, "wb") as fh:
            fh.write(raw)
    if i >= 0 and os.path.exists(dst_path):
        dd = core.read_dsd_info(dst_path)
        print("  patched CMPR -> compression=%r is_dst=%s" % (dd.compression, dd.is_dst))
        check("DST detected from CMPR", dd.is_dst, True)
        p2 = core.plan_to_pcm(sox, ff, dst_path, os.path.join(WORK, "dst.wav"), dd)
        check("DST routed to ffmpeg", p2.tool, "ffmpeg")
        check("ffmpeg command uses -ar", "-ar " in core.build_command(p2), True)
        check("DST route warns about ignored effects",
              any("ffmpeg" in w and "效果" in w or "effects" in w for w in
                  core.plan_to_pcm(sox, ff, dst_path, p2.output_path, dd,
                                   post_effects=["gain", "-1"]).warnings), True)
    else:
        skipped.append("DST routing (could not patch CMPR)")

    section("quoting and argv")
    check("plain path unquoted", core._quote(r"C:\a\b.dsf"), r"C:\a\b.dsf")
    check("spaces quoted", core._quote(r"C:\a b\c.dsf"), '"C:\\a b\\c.dsf"')
    p = core.plan_to_pcm(sox, ff, r"C:\a b\c.dsf", r"C:\d e\f.wav",
                         core.read_dsd_info(dsf64))
    argv = core.build_argv(p)
    check("argv strips the quotes", r"C:\a b\c.dsf" in argv, True)
    check("argv has no quoted literal", '"C:\\a b\\c.dsf"' not in argv, True)

    section("execute a real conversion in each direction")
    out1 = os.path.join(WORK, "run_to_dsd.dsf")
    argv = core.build_argv(core.plan_to_dsd(sox, probes["pcm_tone"], out1,
                                            hdr.get("rate"), sig.peak_db,
                                            dsd_rate=2822400))
    pr = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    made = os.path.exists(out1) and os.path.getsize(out1) > 0
    print("  PCM->DSD exit=%s created=%s size=%s" % (pr.returncode, made,
          os.path.getsize(out1) if made else 0))
    if pr.returncode != 0 or not made:
        failures.append("PCM->DSD conversion failed: %s" % (pr.stderr or "")[:140])
    else:
        back = core.read_dsd_info(out1)
        check("output is a readable DSF", back.ok, True)

    out2 = os.path.join(WORK, "run_to_pcm.wav")
    argv = core.build_argv(core.plan_to_pcm(sox, ff, dsf64, out2,
                                            core.read_dsd_info(dsf64), pcm_rate=88200))
    pr = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    peak = run_peak(sox, out2) if os.path.exists(out2) else None
    print("  DSD->PCM exit=%s decoded peak=%s dBFS" % (pr.returncode, peak))
    # 0.0 dBFS here would mean the raw bitstream was copied instead of decoded
    check("decode peak is sane (not 0.0 = raw-bit copy)",
          peak is not None and peak < -6.0, True)

    shutil.rmtree(WORK, ignore_errors=True)

    print()
    if skipped:
        print("skipped: %s" % skipped)
    print("ALL CHECKS PASSED" if not failures else "FAILURES: %s" % failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
