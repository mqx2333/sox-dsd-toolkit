"""
selftest.py -- smoke test for core.py, using the real sox_ng binary.

Run:  python selftest.py
It measures a real music file if present, builds a synthetic 48 kHz file to exercise
the 48k-family branch, prints the generated commands, and checks the gain maths.
"""

from __future__ import annotations

import os
import sys
import tempfile

import core

MUSIC = r"C:\Users\Administrator\Desktop\Project\MusicTota\赵雷 - 船长.flac"
OUT = r"C:\Users\Administrator\Desktop\Project\MusicTota\赵雷 - 船长_DSD.dsf"

failures = []


def check(label: str, got, want) -> None:
    ok = got == want
    print("  [%s] %s: got %r, want %r" % ("PASS" if ok else "FAIL", label, got, want))
    if not ok:
        failures.append(label)


def section(title: str) -> None:
    print("\n=== %s ===" % title)


def main() -> int:
    section("gain maths")
    check("0 dBFS peak -> 0 dBDSD", core.compute_gain(0.0), -6.0)
    check("-3 dBFS peak", core.compute_gain(-3.0), -3.0)
    check("-12 dBFS peak (quiet master)", core.compute_gain(-12.0), 6.0)
    check("safety margin", core.compute_gain(0.0, safety_margin_db=1.0), -7.0)
    check("SACD ceiling target", core.compute_gain(0.0, target_peak_dbfs=-2.9), -2.9)

    section("family / ratio classification")
    check("44100 -> 44.1k", core.classify_family(44100), "44.1k")
    check("88200 -> 44.1k", core.classify_family(88200), "44.1k")
    check("48000 -> 48k", core.classify_family(48000), "48k")
    check("96000 -> 48k", core.classify_family(96000), "48k")
    check("44100 divides 5644800", core.is_integer_ratio(44100, 5644800), True)
    check("48000 does NOT divide 5644800", core.is_integer_ratio(48000, 5644800), False)
    check("48000 divides 6144000", core.is_integer_ratio(48000, 6144000), True)

    section("sox discovery and probing")
    exe = core.find_sox()
    print("  binary:", exe)
    if not exe:
        failures.append("sox not found")
        return 1
    probe = core.probe_sox(exe)
    print("  version:", probe["version"])
    print("  has sdm:", probe["has_sdm"])
    print("  containers:", probe["containers"])
    print("  dsf rates:", core.target_rates_for(probe["containers"], "dsf"))  # type: ignore[arg-type]
    if not probe["has_sdm"]:
        failures.append("this sox has no sdm effect")

    section("real source file")
    if os.path.isfile(MUSIC):
        header, stats, tp = core.measure_all(MUSIC, exe, truepeak=True)
        print("  header :", header)
        print("  peak   : %.2f dBFS  (L %.2f / R %.2f)" % (stats.peak_db, stats.peak_db_left, stats.peak_db_right))
        print("  rms    : %.2f dBFS   dc %.5f" % (stats.rms_db, stats.dc_offset))
        if stats.clipped_source:
            print("  note   : source sits at digital full scale (likely already clipped)")
        if tp and tp.peak_db is not None:
            print("  truepk : %.2f dBFS (delta %+.2f dB vs sample peak)" % (tp.peak_db, tp.peak_db - stats.peak_db))
        rate = header.get("rate")
        plan = core.build_plan(exe, MUSIC, OUT, rate, 5644800,
                               measured_peak_db=stats.peak_db)
        print("\n  COMMAND:")
        print("  " + core.command_string(plan))
        for n in plan.notes:
            print("   -", n)
        for w in plan.warnings:
            print("   !", w)
    else:
        print("  (music file not present, skipping)")

    section("48 kHz synthetic source (non-integer ratio branch)")
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selftest")
    os.makedirs(tmp, exist_ok=True)
    wav48 = os.path.join(tmp, "t48.wav")
    core._run(exe, ["-n", "-r", "48000", "-c", "2", "-b", "24", wav48,
                    "synth", "1", "sin", "1000", "gain", "-3"])
    header, stats, _ = core.measure_all(wav48, exe)
    print("  header :", header)
    if stats.peak_db is None:
        failures.append("could not measure synthetic 48k file: %s" % stats.error)
    else:
        print("  peak   : %.2f dBFS" % stats.peak_db)
    plan48 = core.build_plan(exe, wav48, os.path.join(tmp, "t48_DSD.dsf"),
                             header.get("rate"), 5644800, measured_peak_db=stats.peak_db)
    print("  use rate effect      :", plan48.use_rate_effect)
    print("  irrational accuracy  :", plan48.use_irrational_accuracy)
    print("  COMMAND:")
    print("  " + core.command_string(plan48))
    if not plan48.use_irrational_accuracy:
        failures.append("48k source should enable -t")
    for w in plan48.warnings:
        print("   !", w)

    section("44.1 kHz synthetic source (integer ratio branch)")
    wav44 = os.path.join(tmp, "t44.wav")
    core._run(exe, ["-n", "-r", "44100", "-c", "2", "-b", "24", wav44,
                    "synth", "1", "sin", "1000", "gain", "-3"])
    header, stats, _ = core.measure_all(wav44, exe)
    if stats.peak_db is None:
        failures.append("could not measure synthetic 44k file: %s" % stats.error)
    else:
        plan44 = core.build_plan(exe, wav44, os.path.join(tmp, "t44_DSD.dsf"),
                                 header.get("rate"), 5644800, measured_peak_db=stats.peak_db)
        print("  peak %.2f dBFS -> gain %+.1f dB" % (stats.peak_db, plan44.gain_db))
        print("  irrational accuracy  :", plan44.use_irrational_accuracy)
        print("  COMMAND:")
        print("  " + core.command_string(plan44))
        if plan44.use_irrational_accuracy:
            failures.append("44.1k source must NOT enable -t")
        if header.get("rate") != 44100:
            failures.append("header rate not read (got %r)" % header.get("rate"))

    section("quoting")
    check("plain path", core._quote(r"C:\a\b.flac"), r"C:\a\b.flac")
    check("spaces", core._quote(r"C:\a b\c.flac"), '"C:\\a b\\c.flac"')

    print("\n" + ("ALL CHECKS PASSED" if not failures else "FAILURES: %s" % failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
