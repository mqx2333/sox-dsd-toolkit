# Convert-Dsd.ps1 -- DSD <-> PCM conversion with a live progress bar.
#
# Two directions, one script:
#   -Direction to-dsd   PCM (flac/wav/...) -> DSD (.dsf/.dff)
#   -Direction to-pcm   DSD (.dsf/.dff)    -> PCM 24-bit (.wav/.flac)
#
# Progress comes from the tools themselves, not from a guess:
#   * sox_ng -S                prints "In: 42%  00:00:12.3 [00:00:17.1] Out:..."
#   * ffmpeg -progress pipe:1  prints out_time_us / out_time / speed / progress
# sox_ng -S is also added to the ffmpeg path when sox_ng can drive it, purely for the
# progress bar; ffmpeg is used when sox_ng cannot read the input (DST-compressed DFF).
#
# Examples:
#   .\Convert-Dsd.ps1 -Direction to-dsd -InputFile "song.flac"
#   .\Convert-Dsd.ps1 -Direction to-pcm -InputFile "album.dsf" -OutputFile "album.wav"
#   .\Convert-Dsd.ps1 -Direction to-pcm -InputFile "x.dff" -TargetRate 176400 -Bits 24
#   .\Convert-Dsd.ps1 -Direction to-dsd -InputFile "song.flac"
#   .\Convert-Dsd.ps1 -Direction to-dsd -InputFile "song.flac" -NoProgress   # plain run
#
# Note: the parameter is -InputFile, not -Input, because $Input is a PowerShell
# automatic variable and would shadow it.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('to-dsd', 'to-pcm')]
    [string]$Direction,

    [Parameter(Mandatory = $true)]
    [string]$InputFile,

    [string]$OutputFile,

    # ---- common ----
    [ValidateSet('auto', '2822400', '5644800', '11289600', '22579200', '3072000', '6144000', '12288000')]
    [string]$DsdRate = 'auto',

    [ValidateSet('24', '16')]
    [int]$Bits = 24,

    [ValidateSet('wav', 'flac', 'dsf', 'dff')]
    [string]$Container = 'auto',

    [string]$Sox,
    [string]$Ffmpeg,
    # force the ffmpeg decoder even for files sox_ng could read (testing/verification)
    [switch]$ForceFfmpeg,
    [switch]$NoProgress,
    [string]$LogDir,
    # Write machine-readable progress here (JSON) so a GUI can drive a progress bar.
    [string]$StatusFile
)

$ErrorActionPreference = 'Stop'

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

function Get-SoxText {
    <#
      Run a sox command and return its combined output as text.

      With $ErrorActionPreference = 'Stop', PowerShell turns a native command's stderr
      into a terminating error, and `2>&1` into the pipeline does not prevent that.
      Redirecting stderr to a temp file at the process level avoids the issue entirely.
    #>
    param([string]$Exe, [string[]]$Arguments)
    $tmp = [IO.Path]::GetTempFileName()
    try {
        $args2 = $Arguments | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }
        $p = Start-Process -FilePath $Exe -ArgumentList $args2 -NoNewWindow -Wait -PassThru `
                           -RedirectStandardError $tmp -RedirectStandardOutput ($tmp + '.out')
        $err = Get-Content -Raw -Path $tmp -ErrorAction SilentlyContinue
        $out = Get-Content -Raw -Path ($tmp + '.out') -ErrorAction SilentlyContinue
        return (("$out`n$err") -as [string])
    } finally {
        Remove-Item $tmp, ($tmp + '.out') -Force -ErrorAction SilentlyContinue
    }
}

function Find-SoxNg {
    param([string]$Explicit)
    if ($Explicit -and (Test-Path $Explicit)) { return $Explicit }
    $c = Join-Path $env:LOCALAPPDATA 'Programs\sox_ng\sox_ng.exe'
    if (Test-Path $c) { return $c }
    foreach ($p in @('C:\Program Files\sox_ng\sox_ng.exe')) { if (Test-Path $p) { return $p } }
    $g = Get-Command sox_ng -ErrorAction SilentlyContinue
    if ($g) { return $g.Source }
    return $null
}

function Find-Ffmpeg {
    param([string]$Explicit)
    if ($Explicit -and (Test-Path $Explicit)) { return $Explicit }
    foreach ($p in @('C:\ffmpeg-2025-07-23-git-829680f96a-full_build\bin\ffmpeg.exe',
                     'C:\ffmpeg\bin\ffmpeg.exe')) {
        if (Test-Path $p) { return $p }
    }
    $g = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($g) { return $g.Source }
    return $null
}

function Format-Duration {
    param([double]$Seconds)
    if ($Seconds -lt 0 -or [double]::IsNaN($Seconds)) { return '--:--' }
    $ts = [TimeSpan]::FromSeconds([math]::Round($Seconds))
    if ($ts.TotalHours -ge 1) { return ('{0:d2}:{1:d2}:{2:d2}' -f $ts.Hours, $ts.Minutes, $ts.Seconds) }
    return ('{0:d2}:{1:d2}' -f $ts.Minutes, $ts.Seconds)
}

function Write-ProgressBar {
    param(
        [string]$Label,
        [double]$Fraction,      # 0..1, -1 = indeterminate
        [string]$Extra = ''
    )
    $width = 32
    if ($Fraction -lt 0) {
        $bar = ' ' * $width
        $pct = '  ?%'
    } else {
        if ($Fraction -gt 1) { $Fraction = 1 }
        if ($Fraction -lt 0) { $Fraction = 0 }
        $filled = [int][math]::Floor($Fraction * $width)
        $bar = ('#' * $filled) + ('-' * ($width - $filled))
        $pct = ('{0,3:0}%' -f ($Fraction * 100))
    }
    $line = "{0} [{1}] {2} {3}" -f $Label, $bar, $pct, $Extra

    if ($script:Redirected) {
        # Output is being captured: a carriage-return bar would be swallowed, so emit
        # periodic whole lines instead (every 5% or on completion).
        $bucket = if ($Fraction -lt 0) { -1 } else { [int][math]::Floor($Fraction * 20) }
        if ($bucket -ne $script:LastBucket) {
            $script:LastBucket = $bucket
            Write-Host $line
        }
        return
    }
    $pad = [Math]::Max(0, $script:LastLen - $line.Length)
    Write-Host ("`r" + $line + (' ' * $pad)) -NoNewline
    $script:LastLen = $line.Length
}

function Write-Status {
    <# Optional machine-readable progress for a GUI to poll. #>
    param([string]$Phase, [double]$Percent = -1, [string]$Detail = '')
    if (-not $script:StatusFile) { return }
    try {
        $o = [ordered]@{
            phase   = $Phase
            percent = if ($Percent -lt 0) { $null } else { [math]::Round($Percent, 1) }
            detail  = $Detail
            updated = (Get-Date).ToString('s')
        }
        [IO.File]::WriteAllText($script:StatusFile, ($o | ConvertTo-Json -Compress))
    } catch { }
}

# --------------------------------------------------------------------------
# input probing
# --------------------------------------------------------------------------

function Get-DsdInfo {
    param([string]$Path)
    $fs = [System.IO.File]::OpenRead($Path)
    try {
        $buf = New-Object byte[] 4096
        $n = $fs.Read($buf, 0, $buf.Length)
    } finally { $fs.Close() }

    $magic = [System.Text.Encoding]::ASCII.GetString($buf, 0, 4)
    $info = @{ Container = 'unknown'; Rate = 0; Channels = 0; Compression = ''; IsDst = $false; Ok = $false }

    if ($magic -eq 'DSD ') {
        # DSF: 'DSD '|size(8)|version(4) then 'fmt '|size(8)|version(4)|format id(4)
        #      |channel type(4)|channel num(4)|sample rate(4)|bits(4)|sample count(8)
        # => channel type @48, channels @52, sample rate @56, bits @60, samples @64
        $info.Container = 'dsf'
        $info.Channels = [BitConverter]::ToInt32($buf, 52)
        $info.Rate = [BitConverter]::ToInt32($buf, 56)
        $info.Ok = ($info.Rate -gt 0) -and ([System.Text.Encoding]::ASCII.GetString($buf, 28, 4) -eq 'fmt ')
    } elseif ($magic -eq 'FRM8') {
        $info.Container = 'dff'
        $text = [System.Text.Encoding]::ASCII.GetString($buf)
        $i = $text.IndexOf('FS  ')
        if ($i -ge 0) {
            # 8-byte big-endian size then the 4-byte rate
            $size = 0; for ($k = 0; $k -lt 8; $k++) { $size = ($size -shl 8) -bor $buf[$i + 4 + $k] }
            $rate = 0; for ($k = 0; $k -lt 4; $k++) { $rate = ($rate -shl 8) -bor $buf[$i + 12 + $k] }
            $info.Rate = $rate
        }
        $i = $text.IndexOf('CHNL')
        if ($i -ge 0) {
            $ch = 0; for ($k = 0; $k -lt 2; $k++) { $ch = ($ch -shl 8) -bor $buf[$i + 12 + $k] }
            $info.Channels = $ch
        }
        $i = $text.IndexOf('CMPR')
        if ($i -ge 0) {
            $info.Compression = $text.Substring($i + 12, 4).Trim()
            $info.IsDst = $info.Compression.StartsWith('DST')
        }
        $info.Ok = $info.Rate -gt 0
    }
    return $info
}

function Get-PcmInfo {
    param([string]$Sox, [string]$Path)
    $out = Get-SoxText -Exe $Sox -Arguments @('--i', $Path)
    $rate = 0; $ch = 0
    if ($out -match '(?m)^\s*Sample Rate\s*:\s*([0-9.e+]+)') {
        $rate = [int][double]::Parse($matches[1], [Globalization.CultureInfo]::InvariantCulture)
    }
    if ($out -match '(?m)^\s*Channels\s*:\s*(\d+)') { $ch = [int]$matches[1] }
    return @{ Rate = $rate; Channels = $ch; Ok = $rate -gt 0 }
}

# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

$script:LastLen = 0
$script:LastBucket = -99
$script:Redirected = [Console]::IsOutputRedirected
$script:StatusFile = $StatusFile
$script:SoxWatch = [Diagnostics.Stopwatch]::StartNew()
$sox = Find-SoxNg -Explicit $Sox
$ffmpeg = Find-Ffmpeg -Explicit $Ffmpeg

if (-not (Test-Path $InputFile)) { throw "input not found: $InputFile" }
$inputPath = (Resolve-Path $InputFile).Path

$tiers = @{
    2822400 = @{ Tier = 'DSD64'; Pcm = 88200 }
    5644800 = @{ Tier = 'DSD128'; Pcm = 176400 }
    11289600 = @{ Tier = 'DSD256'; Pcm = 352800 }
    22579200 = @{ Tier = 'DSD512'; Pcm = 705600 }
    3072000 = @{ Tier = 'DSD64-48k'; Pcm = 96000 }
    6144000 = @{ Tier = 'DSD128-48k'; Pcm = 192000 }
    12288000 = @{ Tier = 'DSD256-48k'; Pcm = 384000 }
}

if ($Direction -eq 'to-pcm') {
    # ---------------- DSD -> PCM ----------------
    $d = Get-DsdInfo -Path $inputPath
    if (-not $d.Ok) { throw "not a DSD file (no DSF/DFF header): $inputPath" }

    $target = if ($DsdRate -ne 'auto') { [int]$DsdRate } else { $tiers[$d.Rate].Pcm }
    if (-not $target) { $target = [int]($d.Rate / 32) }

    if ($Container -eq 'auto') { $Container = 'wav' }
    if ($Container -notin @('wav', 'flac')) { $Container = 'wav' }
    if (-not $OutputFile) {
        $OutputFile = [IO.Path]::ChangeExtension($inputPath, $null).TrimEnd('.') + '_PCM.' + $Container
    }
    if (-not [IO.Path]::IsPathRooted($OutputFile)) { $OutputFile = Join-Path (Get-Location) $OutputFile }
    $outPath = $OutputFile

    $useFfmpeg = $d.IsDst -or $ForceFfmpeg
    if ($d.IsDst -and -not $ffmpeg) {
        throw "this DFF is DST-compressed and sox_ng cannot read it, but ffmpeg was not found"
    }
    if ($ForceFfmpeg -and -not $ffmpeg) { throw 'ffmpeg not found (-ForceFfmpeg)' }

    # total duration for the progress bar (DFF has no sample count: estimate from size)
    $total = 0.0
    if ($d.Container -eq 'dsf') {
        $len = (Get-Item $inputPath).Length
        $total = (($len - 128) * 8.0) / ($d.Rate * [Math]::Max($d.Channels, 1))
    } else {
        $total = ((Get-Item $inputPath).Length * 8.0) / ($d.Rate * [Math]::Max($d.Channels, 1))
    }

    $decoder = if ($useFfmpeg) { 'ffmpeg' } else { 'sox_ng' }
    Write-Host ("DSD -> PCM  : {0} ({1}, {2} Hz, {3}ch{4})" -f `
        (Split-Path $inputPath -Leaf), $d.Container.ToUpper(), $d.Rate, $d.Channels,
        $(if ($d.IsDst) { ', DST' } else { '' })) -ForegroundColor Cyan
    Write-Host ("Target      : {0} Hz / {1}-bit / {2}   via {3}" -f $target, $Bits, $Container.ToUpper(), $decoder) -ForegroundColor Cyan
    Write-Host ("Duration    : ~{0}" -f (Format-Duration $total)) -ForegroundColor Cyan
    Write-Host ""

    if ($useFfmpeg) {
        $codec = if ($Bits -eq 16) { 'pcm_s16le' } else { 'pcm_s24le' }
        $ffArgs = @('-hide_banner', '-loglevel', 'warning', '-stats', '-progress', 'pipe:1',
                    '-y', '-i', $inputPath, '-c:a', $codec, '-ar', "$target", $outPath)
        if ($NoProgress) {
            & $ffmpeg @('-hide_banner', '-loglevel', 'warning', '-y', '-i', $inputPath,
                        '-c:a', $codec, '-ar', "$target", $outPath)
            $exit = $LASTEXITCODE
        } else {
            $psi = New-Object Diagnostics.ProcessStartInfo
            $psi.FileName = $ffmpeg
            $psi.Arguments = ($ffArgs | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' '
            $psi.RedirectStandardOutput = $true
            $psi.RedirectStandardError = $true
            $psi.UseShellExecute = $false
            $psi.CreateNoWindow = $true
            $proc = [Diagnostics.Process]::Start($psi)
            $sw = [Diagnostics.Stopwatch]::StartNew()
            $state = @{}
            while (-not $proc.StandardOutput.EndOfStream) {
                $line = $proc.StandardOutput.ReadLine()
                if (-not $line) { continue }
                $kv = $line -split '=', 2
                if ($kv.Count -eq 2) { $state[$kv[0]] = $kv[1] }
                if ($kv.Count -eq 2 -and $kv[0] -eq 'out_time_us') {
                    $sec = [double]$kv[1] / 1e6
                    $frac = if ($total -gt 0) { $sec / $total } else { -1 }
                    $spd = if ($state['speed']) { $state['speed'].Trim() } else { '?' }
                    $eta = if ($spd -match '([0-9.]+)x' -and [double]$matches[1] -gt 0) {
                        ($total - $sec) / [double]$matches[1]
                    } else { -1 }
                    Write-ProgressBar -Label 'DSD->PCM' -Fraction $frac -Extra (
                        "{0} / {1}  {2}  ETA {3}" -f (Format-Duration $sec), (Format-Duration $total), $spd, (Format-Duration $eta))
                }
            }
            $proc.WaitForExit()
            $exit = $proc.ExitCode
        }
    } else {
        # sox writes its progress line to stderr using carriage returns.
        $soxArgs = @($inputPath, '-t', $Container, '-b', "$Bits", $outPath, 'rate', '-v', "$target")
        if ($NoProgress) {
            & $sox @soxArgs
            $exit = $LASTEXITCODE
        } else {
            # sox writes the progress line to stderr with CR; read it char-wise.
            $psi = New-Object Diagnostics.ProcessStartInfo
            $psi.FileName = $sox
            $psi.Arguments = (@('-S', $inputPath, '-t', $Container, '-b', "$Bits", $outPath,
                                'rate', '-v', "$target") |
                              ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' '
            $psi.RedirectStandardError = $true
            $psi.RedirectStandardOutput = $true
            $psi.UseShellExecute = $false
            $psi.CreateNoWindow = $true
            $proc = [Diagnostics.Process]::Start($psi)
            $script:SoxWatch.Restart()
            $buf = New-Object char[] 512
            while (-not $proc.StandardError.EndOfStream) {
                $n = $proc.StandardError.Read($buf, 0, $buf.Length)
                if ($n -le 0) { break }
                $chunk = -join $buf[0..($n - 1)]
                foreach ($seg in ($chunk -split "[`r`n]+")) {
                    if ($seg -match 'In:\s*(\d+)%\s+([\d:.]+)\s+\[\s*([\d:.]+)\]\s+Out:(\S+)') {
                        $pct = [int]$matches[1]
                        $sec = 0.0
                        $parts = $matches[2].Split(':')
                        if ($parts.Count -eq 3) { $sec = [int]$parts[0]*3600 + [int]$parts[1]*60 + [double]$parts[2] }
                        elseif ($parts.Count -eq 2) { $sec = [int]$parts[0]*60 + [double]$parts[1] }
                        $spdStr = ''
                        $swEl = $script:SoxWatch.Elapsed.TotalSeconds
                        if ($swEl -gt 0.5 -and $sec -gt 0) { $spdStr = ('{0:0.0}x' -f ($sec / $swEl)) }
                        Write-ProgressBar -Label 'DSD->PCM' -Fraction ($pct / 100.0) -Extra (
                            "{0} / {1}  {2}  Out {3}" -f $matches[2], (Format-Duration $total), $spdStr, $matches[4])
                        Write-Status -Phase 'converting' -Percent $pct -Detail ("{0} / {1}" -f $matches[2], (Format-Duration $total))
                    }
                }
            }
            $proc.WaitForExit()
            $exit = $proc.ExitCode
        }
    }
} else {
    # ---------------- PCM -> DSD ----------------
    if (-not $sox) { throw 'sox_ng not found' }
    $p = Get-PcmInfo -Sox $sox -Path $inputPath
    if (-not $p.Ok) { throw "cannot read PCM header: $inputPath" }

    $base = $p.Rate
    $dsd = if ($DsdRate -ne 'auto') { [int]$DsdRate }
           elseif ($base % 44100 -eq 0) { $base * 64 }
           elseif ($base % 48000 -eq 0) { $base * 64 }
           else { 2822400 }
    if ($Container -eq 'auto') { $Container = 'dsf' }
    if ($Container -notin @('dsf', 'dff')) { $Container = 'dsf' }
    if (-not $OutputFile) {
        $OutputFile = [IO.Path]::ChangeExtension($inputPath, $null).TrimEnd('.') + '_DSD.' + $Container
    }
    if (-not [IO.Path]::IsPathRooted($OutputFile)) { $OutputFile = Join-Path (Get-Location) $OutputFile }
    $outPath = $OutputFile

    # gain: place the measured peak on the 0 dBDSD reference (-6 dBFS)
    $stats = Get-SoxText -Exe $sox -Arguments @($inputPath, '-n', 'stats')
    $peak = $null
    if ($stats -match '(?m)^\s*Pk lev dB\s+(\S+)\s+(\S+)\s+(\S+)') { $peak = [double]$matches[3] }
    $gain = if ($null -ne $peak) { [math]::Round(-6.0 - $peak, 1) } else { 0.0 }

    # duration for ETA (from soxi-style length if available)
    $durLine = Get-SoxText -Exe $sox -Arguments @('--i', $inputPath)
    $total = 0.0
    if ($durLine -match '(?m)^\s*Duration\s*:\s*(\d+):(\d+):([\d.]+)') {
        $total = [int]$matches[1] * 3600 + [int]$matches[2] * 60 + [double]$matches[3]
    }

    Write-Host ("PCM -> DSD  : {0} ({1} Hz, {2}ch)" -f (Split-Path $inputPath -Leaf), $p.Rate, $p.Channels) -ForegroundColor Cyan
    Write-Host ("Target      : {0} Hz / {1}   gain {2:+0.0;-0.0;0} dB (peak {3} dBFS)" -f `
        $dsd, $Container.ToUpper(), $gain, $peak) -ForegroundColor Cyan
    Write-Host ("Duration    : ~{0}" -f (Format-Duration $total)) -ForegroundColor Cyan
    Write-Host ""

    $effects = @('gain', "$gain", 'rate', '-v', "$dsd", 'sdm', '-f', 'sdm-6', '-t', '8', '-n', '16', '-l', '512')
    $soxArgs = @($inputPath, '-t', $Container, $outPath) + $effects

    if ($NoProgress) {
        & $sox @($inputPath, '-t', $Container, $outPath, 'gain', "$gain", 'rate', '-v', "$dsd",
                 'sdm', '-f', 'sdm-6', '-t', '8', '-n', '16', '-l', '512')
        $exit = $LASTEXITCODE
    } else {
        $psi = New-Object Diagnostics.ProcessStartInfo
        $psi.FileName = $sox
        $psi.Arguments = (@('-S', $inputPath, '-t', $Container, $outPath, 'gain', "$gain",
                            'rate', '-v', "$dsd", 'sdm', '-f', 'sdm-6', '-t', '8', '-n', '16', '-l', '512') |
                          ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' '
        $psi.RedirectStandardError = $true
        $psi.RedirectStandardOutput = $true
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $proc = [Diagnostics.Process]::Start($psi)
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $buf = New-Object char[] 512
        while (-not $proc.StandardError.EndOfStream) {
            $n = $proc.StandardError.Read($buf, 0, $buf.Length)
            if ($n -le 0) { break }
            $chunk = -join $buf[0..($n - 1)]
            foreach ($seg in ($chunk -split "[`r`n]+")) {
                if ($seg -match 'In:\s*(\d+)%\s+([\d:.]+)\s+\[\s*([\d:.]+)\]\s+Out:(\S+)') {
                    $elapsed = $sw.Elapsed.TotalSeconds
                    $done = [double]$matches[2].Split(':')[-1] + 60 * [double]$matches[2].Split(':')[-2]
                    $eta = if ($done -gt 0.5) { $elapsed * ($total - $done) / $done } else { -1 }
                    Write-ProgressBar -Label 'PCM->DSD' -Fraction ([int]$matches[1] / 100.0) -Extra (
                        "{0} / {1}  ETA {2}  Out {3}" -f (Format-Duration $done), (Format-Duration $total),
                        (Format-Duration $eta), $matches[4])
                }
            }
        }
        $proc.WaitForExit()
        $exit = $proc.ExitCode
    }
}

Write-Host ""
if ($exit -eq 0) {
    $mb = [math]::Round((Get-Item $outPath).Length / 1MB, 1)
    Write-Host ("DONE  ->  {0}  ({1} MB)" -f $outPath, $mb) -ForegroundColor Green
} else {
    Write-Host ("FAILED (exit {0})" -f $exit) -ForegroundColor Red
}
exit $exit
