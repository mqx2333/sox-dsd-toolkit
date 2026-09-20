# verify-dsd.ps1 - validate SoX-produced DSD by decoding it back to PCM.
#
# Why: you cannot look at a DSD bitstream directly. Decode it with sox, band-limit
#      to the audio band, then measure. DSD64 noise shaping pushes quantization
#      noise above 20 kHz, so an unlimited measurement just shows ultrasonic noise.
#
# Usage (Windows PowerShell 5.1: use `powershell -File`; PS7: `pwsh -File`):
#   # end-to-end: synthesize probes, encode to DSD, decode back, measure
#   powershell -File .\verify-dsd.ps1 -Sox "C:\path\to\sox.exe" -Input music.wav
#
#   # measure an existing DSD file
#   powershell -File .\verify-dsd.ps1 -Sox "C:\path\to\sox.exe" -DsdFile out.dsf
#
#   # run a single probe (silence | low | ref)
#   powershell -File .\verify-dsd.ps1 -Sox ... -Input music.wav -Only low
#
# Notes:
#   * Only `sdm` exists in SoX; there is NO `dsd` effect. DSD->PCM works by
#     reading .dsf/.dff natively and resampling down with `rate`.
#   * The output rate (-r) is MANDATORY when reading DSD.  sox unpacks the 1-bit
#     stream to +-full-scale samples at the DSD rate and only auto-inserts `rate`
#     when the output rate differs; without -r it writes the raw bitstream out as
#     8-bit PCM (a square wave, peak 0.00 dBFS).  With -r the decode is correct
#     and matches ffmpeg exactly.
#   * Always pass -Filter explicitly: with no -f, src/sdm.c picks the lowest-order
#     filter that matches the rate (clans-4), silently costing in-band noise.
#   * Trellis is OFF unless -t/-n/-l are given. Defaults when partly enabled:
#     order 13, paths 8, latency 1024. Ranges: order 3-32, paths 4-32, latency 100-2048.
#   * This file is pure ASCII on purpose, so it parses under any code page.

[CmdletBinding()]
param(
    [string]$Sox = "sox",
    [string]$Input,
    [string]$DsdFile,
    [ValidateSet(2822400, 5644800, 11289600)]
    [int]$DsdRate = 2822400,
    [string]$Filter = "clans-8",
    [string]$Trellis = "-t 32 -n 32",
    [string]$WorkDir,
    [ValidateSet("silence", "low", "ref")]
    [string]$Only
)

$ErrorActionPreference = "Stop"

function Info($m) { Write-Host "[*] $m" }
function Ok($m)   { Write-Host "[ok] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Bad($m)  { Write-Host "[x] $m" -ForegroundColor Red }

# Optional message catalogue so the console can stay ASCII by default.
# Set $env:DSDVERIFY_LANG = 'zh' for Chinese output (file must then be saved as UTF-8 with BOM
# for PowerShell 5.1 to decode the characters correctly).
$zh = @{
    noSdm   = "sdm effect missing: stock sox 14.4.2 has no DSD support; use sox_ng / sox-dsd-win / mansr branch."
    hasSdm  = "sdm effect present."
    cantEnc = "This sox cannot encode DSD (no sdm). Use -DsdFile to verify an existing DSD file."
}
$useZh = ($env:DSDVERIFY_LANG -eq 'zh')

# ---------- 0. environment / capability probe ----------
if (-not (Get-Command $Sox -ErrorAction SilentlyContinue)) {
    if (-not (Test-Path $Sox)) { throw "sox not found: $Sox" }
}
$soxVer = (& $Sox --version 2>&1 | Out-String).Trim()
Info "sox: $soxVer"

$helpText = (& $Sox --help 2>&1 | Out-String)
$hasSdm   = $helpText -match '\bsdm\b'
$fmtText  = (& $Sox --help-format all 2>&1 | Out-String)
$hasDff   = $fmtText -match '\bdff\b'
$hasDsf   = $fmtText -match '\bdsf\b'

if ($hasSdm) { Ok $(if ($useZh) { $zh.hasSdm } else { "sdm effect: present" }) }
else         { Bad $(if ($useZh) { $zh.noSdm }  else { "sdm effect: MISSING (stock sox 14.4.2 has no DSD support)" }) }

$containers = @()
if ($hasDff) { $containers += 'dff' }
if ($hasDsf) { $containers += 'dsf' }
if ($containers.Count -gt 0) { Ok "DSD containers: $($containers -join ' ')" }
else { Warn "DSD containers: none detected (use -t raw and wrap it yourself)" }

if (-not $hasSdm -and -not $DsdFile) {
    throw $(if ($useZh) { $zh.cantEnc } else { "This sox cannot encode DSD (no sdm effect)." })
}

# Dump the local sdm option table - the only authoritative source for your build.
try {
    $sdmHelp = (& $Sox --help-effect sdm 2>&1 | Out-String).Trim()
    if ($sdmHelp) { Info "local sdm usage: $($sdmHelp -replace '\s+', ' ')" }
} catch { Warn "could not read --help-effect sdm" }

if (-not $WorkDir) {
    $base = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
    $WorkDir = Join-Path $base "work"
}
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
Info "work dir: $WorkDir"

$probeRate = 44100
$bandLow, $bandHigh = 20, 20000

# ---------- 1. probe signals ----------
# Levels are PCM dBFS. 0 dBDSD = 50% modulation = -6 dBFS PCM.
# SACD peak limit: +3.1 dBDSD ~= -2.9 dBFS PCM.
$signals = @(
    [pscustomobject]@{ Key = 'silence'; PcmDbfs = $null;  Frequency = 1000; Desc = 'silence -> in-band noise floor + idle tones' },
    [pscustomobject]@{ Key = 'low';     PcmDbfs = -60.0;  Frequency = 1000; Desc = '-60 dBFS 1 kHz -> idle tones / low-level distortion' },
    [pscustomobject]@{ Key = 'ref';     PcmDbfs = -6.0;   Frequency = 1000; Desc = '-6 dBFS 1 kHz (= 0 dBDSD) -> reference level' }
)
if ($Only) { $signals = $signals | Where-Object { $_.Key -eq $Only } }

# ---------- 2. helpers ----------
function Invoke-Sox {
    param([string[]]$SoxArgs, [switch]$AllowFail)
    $out = & $Sox @SoxArgs 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0 -and -not $AllowFail) {
        throw "sox failed (exit $LASTEXITCODE): $Sox $($SoxArgs -join ' ')`n$out"
    }
    return [pscustomobject]@{ Text = $out; Exit = $LASTEXITCODE }
}

function Get-StatField {
    param([string]$Text, [string]$Label, [switch]$Last)
    $pat = "(?m)^\s*$([regex]::Escape($Label))\s+(-?[\d\.\-eE\+]+|nan|inf|-inf)\s*$"
    $ms = [regex]::Matches($Text, $pat)
    if ($ms.Count -eq 0) { return $null }
    $v = if ($Last) { $ms[$ms.Count - 1].Groups[1].Value } else { $ms[0].Groups[1].Value }
    if ($v -match 'nan|inf') { return $null }
    return [double]$v
}

function Measure-Band {
    # DSD -> PCM at an intermediate rate -> 44.1k -> band-limit -> stats
    param([string]$Dsd, [string]$Tag)

    $midRate = $DsdRate
    while ($midRate -gt 705600) { $midRate = [int]($midRate / 2) }

    $dec = Join-Path $WorkDir "$Tag.dec.wav"
    Invoke-Sox -SoxArgs @($Dsd, '-t', 'wav', '-e', 'signed-integer', '-b', '24', '-r', "$midRate", '-D', $dec) | Out-Null

    $cd = Join-Path $WorkDir "$Tag.44k.wav"
    Invoke-Sox -SoxArgs @($dec, $cd, 'rate', '-v', '44100') | Out-Null

    $band = Join-Path $WorkDir "$Tag.band.wav"
    $r = Invoke-Sox -SoxArgs @($cd, '-t', 'wav', '-e', 'float', '-b', '32', $band,
                               'sinc', "$bandLow-$bandHigh", 'stats') -AllowFail

    return [pscustomobject]@{
        Decoded = $cd
        Band    = $band
        RmsDb   = Get-StatField -Text $r.Text -Label 'RMS     amplitude' -Last
        PeakDb  = Get-StatField -Text $r.Text -Label 'Maximum amplitude' -Last
        DcDb    = Get-StatField -Text $r.Text -Label 'DC offset' -Last
        Raw     = $r.Text
    }
}

function Encode-Dsd {
    param([string]$Wav, [string]$Tag)
    $ext  = if ($hasDsf) { 'dsf' } elseif ($hasDff) { 'dff' } else { $null }
    $out  = if ($ext) { Join-Path $WorkDir "$Tag.dsd.$ext" } else { Join-Path $WorkDir "$Tag.dsd.raw" }
    $rate = "$DsdRate"

    $extra = @()
    if ($Trellis -and $Trellis.Trim()) { $extra = $Trellis.Trim() -split '\s+' }

    if ($ext) {
        $a = @($Wav, $out, 'rate', '-v', $rate, 'sdm', '-f', $Filter) + $extra
    } else {
        $a = @($Wav, '-t', 'raw', '-e', 'signed', '-b', '1', '-r', $rate, '-c', '2', $out,
               'rate', '-v', $rate, 'sdm', '-f', $Filter) + $extra
    }
    $r = Invoke-Sox -SoxArgs $a
    return [pscustomobject]@{ File = $out; Text = $r.Text; ClipWarning = ($r.Text -match '(?i)clip') }
}

# ---------- 3. main loop ----------
$results = @()

foreach ($s in $signals) {
    Write-Host ""
    Info "=== $($s.Key): $($s.Desc) ==="

    $tag   = "probe-$($s.Key)"
    $probe = Join-Path $WorkDir "$tag.wav"

    if ($DsdFile) {
        $dsd = $DsdFile
        Info "using existing DSD file: $dsd"
    } else {
        $a = @('-n', '-r', "$probeRate", '-c', '2', '-e', 'signed-integer', '-b', '24', $probe,
               'synth', '10', 'sin', "$($s.Frequency)", 'gain', '-6')
        if ($null -ne $s.PcmDbfs -and $s.PcmDbfs -ne -6.0) {
            $a += @('gain', ("{0}" -f ($s.PcmDbfs + 6.0)))
        }
        Invoke-Sox -SoxArgs $a | Out-Null

        $st = Invoke-Sox -SoxArgs @($probe, '-n', 'stats') -AllowFail
        Info ("probe source RMS dBFS: " + (Get-StatField -Text $st.Text -Label 'RMS     amplitude' -Last))

        $enc = Encode-Dsd -Wav $probe -Tag $tag
        if ($enc.ClipWarning) { Warn "sox reported clipping during encode - modulator may be overloaded" }
        $dsd = $enc.File
        $mb = [math]::Round((Get-Item $dsd).Length / 1MB, 2)
        Info "DSD: $dsd ($mb MB)"
    }

    $m = Measure-Band -Dsd $dsd -Tag $tag
    Write-Host $m.Raw

    if ($null -ne $m.DcDb -and [math]::Abs($m.DcDb) -gt -60) {
        Warn "decoded DC offset is high ($([math]::Round($m.DcDb,1)) dB) - check source DC / highpass 10"
    }

    switch ($s.Key) {
        'silence' {
            if ($null -ne $m.RmsDb) {
                if     ($m.RmsDb -lt -120) { Ok  "in-band noise floor $([math]::Round($m.RmsDb,1)) dB: good" }
                elseif ($m.RmsDb -lt -100) { Warn "in-band noise floor $([math]::Round($m.RmsDb,1)) dB: marginal (check -f order, trellis, extra dither)" }
                else                       { Bad  "in-band noise floor $([math]::Round($m.RmsDb,1)) dB: bad (unstable modulator or source DC?)" }
            }
        }
        'low' {
            if ($null -ne $m.RmsDb) {
                Info "in-band RMS $([math]::Round($m.RmsDb,1)) dB (expect near -63 dB); a big positive deviation means idle tones / noise modulation"
            }
        }
        'ref' {
            if ($null -ne $m.RmsDb) {
                Info "in-band RMS $([math]::Round($m.RmsDb,1)) dB (expect near -9 dB = RMS of a -6 dBFS sine); THD needs an FFT tool"
            }
        }
    }

    $results += [pscustomobject]@{
        Signal    = $s.Key
        BandRmsDb = if ($null -ne $m.RmsDb) { [math]::Round($m.RmsDb, 1) } else { $null }
        BandPeak  = if ($null -ne $m.PeakDb) { [math]::Round($m.PeakDb, 1) } else { $null }
        Decoded   = $m.Decoded
        Dsd       = $dsd
    }
}

# ---------- 4. summary ----------
Write-Host ""
Info "=== summary ==="
$results | Format-Table -AutoSize
Write-Host @"
Manual review checklist:
  1) Silence in-band noise floor: DSD64 + clans-8 should land well below -120 dB;
     higher orders / higher DSD rates do better.
  2) Idle tones: open the decoded wav in a spectrum tool and look for discrete
     spikes inside 20 Hz - 20 kHz.
  3) THD: harmonics of the -6 dBFS 1 kHz tone should sit under the noise floor.
  4) Overload: re-test anything whose PCM peak exceeds -2.9 dBFS (+3.1 dBDSD).
  5) DC: decoded DC offset should be ~0.
  6) A/B the trellis settings (-t 8 -n 8 vs -t 32 -n 32 vs no trellis): the manual
     warns results are unpredictable, so confirm by measurement, not by reputation.
Intermediate files: $WorkDir
"@
