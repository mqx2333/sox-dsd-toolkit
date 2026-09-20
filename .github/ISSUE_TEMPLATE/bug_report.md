---
name: Bug report
about: Something produced the wrong result, or the toolkit would not run
title: "[Bug] "
labels: bug
---

## What happened

<!-- What did you run, and what did it do? -->

## What you expected

<!-- e.g. "a 24-bit WAV at 176.4 kHz", "the gain should have been -3 dB" -->

## Command or UI state

<!--
  If you used the CLI, paste the exact command.
  If you used the GUI, say which direction and which settings, then click
  "Copy command" and paste the generated command line here.
-->

```powershell

```

## Environment

```
sox_ng --version :
ffmpeg -version  :
python --version :
Windows version  :
```

## Source file details

```
sox_ng --i "<your input file>"
```

<!-- The sample rate, channel count and container line are the useful parts. -->

## Anything else

<!--
  Useful extras:
  - does the toolkit warn about anything?
  - does the other engine behave the same way (add -ForceFfmpeg for DSD -> PCM)?
  - is the input DST-compressed? Look for a CMPR chunk in the DFF header.
-->
