# sox-dsd-toolkit

**A measurement-driven PCM ↔ DSD conversion toolkit built on [sox_ng](https://codeberg.org/sox_ng/sox_ng).**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/Python-3.8%2B-blue.svg)](https://www.python.org/)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)](#limitations)
[![sox_ng 14.6+](https://img.shields.io/badge/sox__ng-14.6%2B-informational.svg)](https://codeberg.org/sox_ng/sox_ng)

Most DSD conversion advice online is folklore: copy a command line someone posted, and
hope. This project takes the opposite approach. Every default it applies — the gain, the
target sample rate, the noise-shaping filter, which decoder to use — comes from a
measurement recorded in the documentation, and the toolkit tells you *why* it chose what
it chose, right in the UI.

It ships as a desktop GUI for everyday use and as a PowerShell CLI for scripting.

---

## What it does

| Direction | Input | Output | Engine |
|---|---|---|---|
| **PCM → DSD** | `.flac` `.wav` `.aiff` `.alac` `.ape` `.wv` … | `.dsf` `.dff` | sox_ng (`rate` + `sdm`) |
| **DSD → PCM** | `.dsf` `.dff` `.wsd` | `.wav` `.flac` (24-bit) | sox_ng, or ffmpeg for DST-compressed DFF |

The GUI detects the direction from the input file's extension and shows only the controls
that matter for it. The CLI does both directions and draws a live progress bar.

---

## Why this exists

Sample rate conversion between PCM and DSD has a few traps that silently ruin the result
instead of failing loudly. This toolkit handles them:

**DSD→PCM without an output rate produces garbage, not an error.**
sox_ng's DSD reader unpacks each 1-bit sample to a full-scale value at the DSD rate and
leaves conversion to the ordinary resampler — which SoX only inserts automatically when
the *output* rate differs. Omit it and you get the raw bitstream written out as 8-bit PCM:
a square wave at 2.8224 MHz with a peak of exactly 0.00 dBFS. Nothing warns you.

**PCM→DSD at full scale overloads the modulator, invisibly.**
sox's `sdm` scales its input by 0.5 internally, so a PCM peak of 1.0 drives the modulator
to full scale, well past its stable range (about 0.71, i.e. the SACD peak limit of
+3.1 dBDSD). PCM-domain clip counters never report it; you hear it later as raised in-band
noise and idle tones. The toolkit measures the source peak and computes the gain.

**16-bit output throws away most of DSD's dynamic range.**
Measured cost, in-band noise: **13.6 dB** (DSD64), **17.9 dB** (DSD128), **22.3 dB**
(DSD256). 24-bit is the floor, not a preference.

**Dropping `-f` silently degrades the noise shaper.**
With no filter named, `sdm_find_filter(NULL, rate)` returns the lowest-order table entry
(`clans-4`) and says nothing. The toolkit always names one.

**Some DFF files can't be read by sox_ng at all.**
DST-compressed DFF — common in downloaded material — makes sox_ng fail with
`unsupported compression`. The toolkit reads the `CMPR` chunk, detects it, and routes
those files to ffmpeg, whose decoder was measured equivalent.

---

## Highlights

- **Gain computed, never guessed.** `gain = −6.0 dBFS − measured peak`, placing the
  signal on the 0 dBDSD reference. Shown with its own arithmetic in the UI.
- **Resampling decided, not hardcoded.** Integer ratios skip the accuracy flag;
  48 kHz-family sources are flagged; the target rate is chosen for filter headroom.
- **Correct target rate per tier.** DSD64 → 88.2 kHz, DSD128 → 176.4 kHz,
  DSD256 → 352.8 kHz (48 kHz family → 96/192/384 kHz).
- **Live progress from the tools themselves** — sox_ng's `-S` line, ffmpeg's
  `-progress` stream. No percentage guessing.
- **Verification built in.** Copy the command, or run it and measure the output's
  in-band level in one click.
- **Headless DSD header parsing.** DSF/DFF rate, channel count and compression are read
  in pure Python — no `ffprobe` dependency.
- **No third-party Python packages.** Standard library and tkinter only.

---

## Requirements

| Component | Needed for | Notes |
|---|---|---|
| **Python 3.8+** with tkinter | the GUI | bundled with the official Windows installer |
| **[sox_ng](https://codeberg.org/sox_ng/sox_ng) 14.6.0 or later** | everything | stock SoX 14.4.2 **cannot read DSD** at all (`no handler for file extension 'dsf'`); DSD read/write arrived in sox_ng 14.6.0 |
| **[ffmpeg](https://ffmpeg.org/)** | DST-compressed DFF only | optional if all your files are DSF or uncompressed DFF |

Prebuilt Windows binaries for sox_ng are published on its
[releases page](https://codeberg.org/sox_ng/sox_ng/releases). The toolkit looks in the
usual install locations first and lets you point at a custom path from the UI.

---

## Usage

### GUI

```
dsd-convert\run.bat
```

Select a source file. The direction, output path, target rate and settings all follow
automatically, and the source is measured immediately. Then:

- **📋 Copy command** — the generated command line, ready to paste
- **Run conversion** — executes it with a live progress bar
- **Verify output** — measures the result's in-band level

### CLI

```powershell
# PCM -> DSD (target and gain decided automatically)
.\Convert-Dsd.ps1 -Direction to-dsd -InputFile "song.flac"

# DSD -> PCM (target chosen from the DSD tier)
.\Convert-Dsd.ps1 -Direction to-pcm -InputFile "album.dsf"

# explicit target, or a plain run without the progress bar
.\Convert-Dsd.ps1 -Direction to-pcm -InputFile "x.dsf" -DsdRate 176400
.\Convert-Dsd.ps1 -Direction to-dsd -InputFile "song.flac" -NoProgress
```

Progress looks like this, and comes from the tools' own output:

```
PCM->DSD [################################] 100% 00:30 / 00:30  ETA 00:00  Out 84.1M
DSD->PCM [################################] 100% 00:04 / 00:04  5.3x  Out 353k
```

The GUI drives the same script and reads its JSON status file, so both front ends share
one implementation.

### Commands it generates

```powershell
# PCM -> DSD
sox_ng.exe "in.flac" -t dsf "out.dsf" gain -6 rate -v 2822400 sdm -f sdm-6 -t 8 -n 16 -l 512

# DSD -> PCM
sox_ng.exe "in.dsf" -t wav -e signed-integer -b 24 "out.wav" rate -v 88200

# DST-compressed DFF -> PCM
ffmpeg.exe -hide_banner -loglevel warning -y -i "in.dff" -c:a pcm_s24le -ar 176400 "out.wav"
```

---

## Notes on DSD

### The bit-rate argument doesn't work the way it looks

It is tempting to compute that DSD64's 2.8224 Mbit/s per channel cannot fit into
88.2 kHz / 24-bit PCM at 2.117 Mbit/s. The comparison does not hold, because DSD's bit
rate and PCM's bit rate are not the same kind of quantity.

- **DSD bits are strongly correlated.** A silence encode measures **3.90 bits of entropy
  per byte (48.8% of raw)**. Much of the bit rate is redundancy. PCM bits are independent
  symbols.
- **Most of the DSD bit rate is ultrasonic noise, not signal.** Encoding the same sine at
  different levels and decoding at the native rate:

  | Input level | In band (20 Hz–20 kHz) | Above 20 kHz |
  |---|---|---|
  | 0 dBFS | 99.35% | 0.655% |
  | −6 dBFS | 97.73% | 2.27% |
  | **−20 dBFS** | **63.4%** | **36.6%** |
  | −60 dBFS | 0.017% | **99.98%** |

  The modulator's total quantisation noise is fixed and does not follow signal level, so
  the quieter the programme material, the larger the noise share.
- **The audible information fits comfortably.** 20 Hz–20 kHz needs a little over 40 kHz
  of sampling by Nyquist; 88.2 kHz has more than double the margin. 24-bit provides about
  146 dB of dynamic range against DSD64's measured in-band floor of roughly −116 dB.

What a DSD64 → 44.1/88.2 kHz conversion discards is the ultrasonic noise that noise
shaping deliberately put there — which is also why every DSD DAC needs a reconstruction
filter. **If the source is DSD128, use 176.4 kHz, not 88.2 kHz**; otherwise the 20–44.1 kHz
region is lost. The GUI picks this automatically from the source rate.

### sox_ng and ffmpeg decode DSD equivalently

Measured, not assumed:

| Test | Result |
|---|---|
| Six-tone probe, 100 Hz – 18 kHz | **0.00 dB** difference at every tone |
| Per-band spectra, 20 Hz – 20 kHz | within **1 dB** in every band |
| Full-bandwidth RMS of a decoded silence | −85.72 vs −85.67 dBFS |
| Band-edge transparency (997 Hz / 20 kHz) | −12.01 / −11.75 vs −12.00 / −11.93 dBFS |

Since the decoders are equivalent, the toolkit routes by *capability* rather than
preference: sox_ng by default, ffmpeg where sox_ng cannot read the file at all.

One practical reason to prefer sox_ng where possible: its resampler publishes its stopband
rejection (125 dB at `-h`, **175 dB at `-v`**, 200 dB at `-u`), which matters because a
DSD64 → 44.1 kHz conversion must suppress steeply rising ultrasonic noise with only 2 kHz
of transition band. ffmpeg's DSD path does not expose its filter parameters.

---

## Repository layout

```
├── dsd-convert/            Main application
│   ├── gui.py              tkinter GUI, both directions
│   ├── core.py             All logic: parsing, planning, commands, measurement
│   ├── selftest.py         Smoke tests for both directions
│   ├── run.bat             Launcher
│   └── _legacy/            The two single-direction programs this replaced
├── Convert-Dsd.ps1         CLI converter with progress bar, both directions
├── verify-dsd.ps1          Decode-back verification of a DSD encode
├── README.md               This file
├── OPTIMIZE.md             Where the quality ceiling is, and what to do about it
└── Progress-README.md      Progress-bar CLI reference
```

`OPTIMIZE.md` is worth reading if you want to push past these defaults: it records what
sox's modulator can and cannot do (the noise-shaping filter table is hardcoded, with one
set of coefficients per rate tier, so there is no custom noise transfer function), and
what a move to a purpose-written modulator would involve.

---

## Testing

```powershell
cd dsd-convert
python selftest.py
```

Self-contained: every probe it needs is synthesised locally with sox_ng into a
`_selftest` folder, so it runs on a fresh clone with nothing but the tools installed
(and removes that folder afterwards).

Around 45 checks covering direction detection, gain arithmetic, sample-rate family and
ratio handling, DSF/DFF/DST header parsing, command construction and argument order,
container handling, decoder routing, quoting, and a real conversion executed in each
direction. The final DSD→PCM check asserts that the decoded peak is *not* 0.0 dBFS —
that value would mean the raw bitstream was copied rather than decoded.

---

## Limitations

- **Verification reports a level, not a verdict.** "Verify output" measures the in-band
  level of the result. On a music file that is programme level; on a silence probe it is
  the noise floor. It cannot tell you which you have.
- **ffmpeg cannot run sox effects**, so on the DST route the extra-effects field is
  ignored (the UI says so).
- **Multichannel DSD** is normally stored as one file per channel; this toolkit writes a
  single multichannel file.
- **DFF headers carry no sample count**, so duration is estimated from file size. DSF uses
  the exact value from its header.
- **Container metadata is not written.** Tags (ID3v2 and friends) need a separate tool.
- **Windows-oriented.** The Python application is portable, but the CLI wrapper is
  PowerShell and the default tool paths are Windows ones.

---

## Acknowledgements

Built on [sox_ng](https://codeberg.org/sox_ng/sox_ng), the maintained SoX fork, whose DSD
support (reader, `sdm` modulator and trellis search) is by Måns Rullgård, and on
[ffmpeg](https://ffmpeg.org/) for DST decoding. The DSD level conventions used here —
0 dBDSD as 50% modulation, and the +3.1 dBDSD SACD peak limit — follow the Scarlet Book.

## License

[MIT](LICENSE)
