# Convert-Dsd.ps1 — DSD ↔ PCM 转换，带实时进度条

一个 PowerShell 脚本，两个方向，**进度来自工具本身而不是猜测**：

| 方向 | 引擎 | 进度来源 |
|---|---|---|
| DSD → PCM | sox_ng | `sox_ng -S` 的 `In: 42%` |
| DSD → PCM（DST 压缩的 DFF） | ffmpeg | `ffmpeg -progress` 的 `out_time_us` / `speed` |
| PCM → DSD | sox_ng | `sox_ng -S` 的 `In: 42%` |

---

## 用法

```powershell
# DSD -> PCM（自动定目标：DSD64->88.2k、DSD128->176.4k、DSD256->352.8k）
.\Convert-Dsd.ps1 -Direction to-pcm -InputFile "专辑.dsf"

# DSD -> PCM，指定输出与位深
.\Convert-Dsd.ps1 -Direction to-pcm -InputFile "x.dff" -OutputFile "x.wav" -Bits 24

# DST 压缩的 DFF 自动走 ffmpeg（sox_ng 读不了 DST）
.\Convert-Dsd.ps1 -Direction to-pcm -InputFile "赵雷 - 船长.dff"

# PCM -> DSD（自动定目标：44.1k 家族 -> DSD64，并自动计算增益）
.\Convert-Dsd.ps1 -Direction to-dsd -InputFile "歌曲.flac"

# 不加进度条，纯输出
.\Convert-Dsd.ps1 -Direction to-dsd -InputFile "歌曲.flac" -NoProgress
```

### 参数

| 参数 | 说明 |
|---|---|
| `-Direction` | `to-pcm` 或 `to-dsd`（必填） |
| `-InputFile` | 源文件（必填）。**不叫 `-Input`**，因为 `$Input` 是 PowerShell 自动变量 |
| `-OutputFile` | 输出文件；省略则自动生成 `原名_PCM.wav` / `原名_DSD.dsf` |
| `-DsdRate` | 目标 DSD 速率（`to-dsd`）或换算成 PCM 速率（`to-pcm`）；默认 `auto` |
| `-Bits` | `24`（默认，推荐）或 `16` |
| `-Container` | `wav` / `flac`（to-pcm）或 `dsf` / `dff`（to-dsd）；默认 `auto` |
| `-Sox` / `-Ffmpeg` | 手动指定可执行文件路径 |
| `-ForceFfmpeg` | 强制用 ffmpeg 解码（用于验证两条路径一致） |
| `-NoProgress` | 关闭进度条 |
| `-StatusFile` | 把进度写成 JSON 供 GUI 轮询 |

### 进度条长这样

```
PCM->DSD [################################] 100% 00:30 / 00:30  ETA 00:00  Out 84.1M
DSD->PCM [################################] 100% 00:00:04.00 / 00:04  5.2x  Out 353k
```

显示：百分比 + 进度条、已处理/总时长、速度或 ETA、已写出样本数。
**输出被重定向或捕获时自动改为每 5% 打印一行**（否则回车刷新的进度条会被吞掉）。

---

## 它自动做的判断

**DSD → PCM**
- 读 DSF/DFF 头得到真实速率 → 目标取 **2× 基率**（88.2k / 176.4k / 352.8k）
- 读 DFF 的 `CMPR` 块识别 **DST 压缩** → 自动改用 ffmpeg（sox_ng 会报 `unsupported compression`）
- 24-bit 为默认；`-Bits 16` 允许但会有质量提示

**PCM → DSD**
- 读源采样率 → 44.1k/48k 家族取 ×64（DSD64）
- **自动计算增益**：`gain = -6.0 - 实测峰值`，把峰值放到 0 dBDSD 参考电平
  （PCM 满幅进调制器必然过载——`sdm` 内部按 ×0.5 缩放，稳定上限约 ±0.71）

---

## 给 GUI 用

两个 GUI 的「直接执行转换」按钮就是调用这个脚本，并通过 `-StatusFile` 拿到进度
驱动界面上的进度条：

```powershell
.\Convert-Dsd.ps1 -Direction to-pcm -InputFile x.dsf -OutputFile x.wav -StatusFile progress.json
# progress.json 内容（转换过程中不断更新）：
# {"phase":"converting","percent":42,"detail":"00:01:12.4 / 00:02:51","updated":"..."}
```

GUI 位置：`dsd-to-pcm\run.bat`（DSD→PCM）、`dsd-converter\run.bat`（PCM→DSD）。

---

## 注意事项

- **需要 PowerShell 5.1 或更高**（Windows 自带即可，用的是 `powershell` 而非 `pwsh`）。
- 脚本用 `Start-Process` + 重定向文件来读原生命令的 stderr：因为
  `$ErrorActionPreference='Stop'` 会把原生命令的 stderr 当成终止性错误，
  而 `2>&1` 进管道并不能阻止这一点。
- **PCM → DSD 的 trellis 参数目前是固定的 `sdm-6 -t 8 -n 16 -l 512`**；
  要调 trellis 或换滤波器，用 `dsd-converter` 的 GUI 生成指令后手动执行。
- DSD → PCM 不做任何 DSD 域的"降噪"处理：超声噪声是 DSD 的固有结构，
  处理它应该靠重建滤镜和播放端，不靠事后加工。

---

## 相关文件

| 路径 | 作用 |
|---|---|
| `Convert-Dsd.ps1` | 本脚本（带进度条的双向转换） |
| `dsd-to-pcm\` | DSD→PCM 指令生成器 GUI（进度条已接本脚本） |
| `dsd-converter\` | PCM→DSD 指令生成器 GUI |
| `..\sox-audit\AUDIT.md` | SoX 系列完整性审计 |
