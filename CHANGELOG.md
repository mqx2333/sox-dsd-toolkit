# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-09-20

First release.

### Added

- **`dsd-convert/`** — a desktop GUI for both conversion directions. The direction is
  inferred from the input file's extension and only the controls that apply to it are
  shown; the source is measured as soon as it is selected.
- **`Convert-Dsd.ps1`** — a bidirectional command-line converter with a live progress
  bar, driven by each tool's own progress output (`sox_ng -S`, `ffmpeg -progress`).
  Emits a JSON status file so the GUI can share one implementation with it.
- **`verify-dsd.ps1`** — decode-back verification of a DSD encode.
- **Gain computed from measurement** for PCM → DSD:
  `gain = −6.0 dBFS − measured peak`, placing the signal on the 0 dBDSD reference.
  The UI shows the arithmetic behind the value.
- **Decoder routing by capability** for DSD → PCM: sox_ng normally, ffmpeg for
  DST-compressed DFF (which sox_ng refuses with `unsupported compression`).
- **Pure-Python DSD header parsing** — DSF and DFF rate, channel count and compression
  are read directly, so no `ffprobe` is required.
- **Self-contained selftest** (56 checks) that synthesises every probe it needs, and
  executes a real conversion in each direction.
- **Documentation of the measurements** behind every default, in `README.md` and
  `OPTIMIZE.md`, including the DSD bit-rate explanation and the measured equivalence of
  the sox_ng and ffmpeg decoders.

[1.0.0]: https://github.com/mqx2333/sox-dsd-toolkit/releases/tag/v1.0.0
