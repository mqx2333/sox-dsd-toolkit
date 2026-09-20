# DSD → PCM 最高质量指令生成器（sox_ng）

一个 tkinter GUI：选 DSD 源文件（`.dsf`/`.dff`），读取容器头得到真实 DSD 速率，
自动给出最高质量目标（**2× 基率 / 24-bit**），生成一条可一键复制的转换指令，
并可直接执行、验证产出。

与 `dsd-converter`（PCM→DSD）同构。**解码引擎是 sox_ng**。

---

## 运行

```powershell
# 双击
run.bat

# 或
python gui.py
```

无第三方依赖，只需 Python 3.8+ 自带 tkinter。

---

## 生成的指令形态

```powershell
# DSF / 非压缩 DFF（sox_ng 解码）
sox_ng.exe "输入.dsf" -t wav -e signed-integer -b 24 "输出.wav" rate -v 88200

# DST 压缩的 DFF（sox_ng 拒绝，改用 ffmpeg）
ffmpeg.exe -hide_banner -loglevel warning -y -i "输入.dff" -c:a pcm_s24le -ar 176400 "输出.wav"
```

命令顺序不能换：**输出格式选项紧贴输出文件名**，所有效果（含 `rate`）在其后；
`rate` 效果必须以频率参数收尾。

---

## 关键坑：输出速率是必需的

sox_ng 的 DSD 读取器按设计**只把 1-bit 展开成 ±满幅样本、速率标为 DSD 速率**，
PCM 转换交给普通的 `rate` 重采样器。而 SoX **只在输出速率与输入速率不同时**才自动插入 `rate`：

| 调用 | 结果 |
|---|---|
| `sox_ng x.dsf -t wav -e signed -b 24 out.wav` | 峰值 **0.00 dBFS**，全部样本削波 —— 1-bit 流被当 8-bit PCM 原样写出 |
| `sox_ng x.dsf -t wav -e signed -b 24 out.wav rate -v 88200` | 峰值 **−12.00 dBFS** ✅ |
| `ffmpeg -i x.dsf -c:a pcm_s24le -ar 88200 out.wav` | 峰值 −12.00 dBFS ✅ 与上面一致 |

---

## 为什么大部分文件用 sox_ng，DST 的用 ffmpeg

**按能力路由，不牺牲质量** —— 两个解码器实测等价：

| 检验 | 结果 |
|---|---|
| 六音探针（100 Hz / 500 / 2k / 6k / 12k / 18k） | sox_ng 与 ffmpeg 每个音的差值均为 **0.00 dB** |
| 静音探针逐频段光谱（20–100 … 18k–20k） | 每个频段差 **< 1 dB**；全带宽 RMS −85.72 vs −85.67 dBFS |

**但 sox_ng 拒绝 DST 压缩的 DFF**：

```
sox_ng "x.dff" ... → FAIL formats: can't open input file `x.dff': unsupported compression
ffmpeg  "x.dff" ... → exit 0，输出正常
```

DST 是 DSD 的无损压缩（把 DFF 里的 1-bit 数据压掉约一半），常见于网上下载的 DFF。
程序会读 DFF 头里的 `CMPR` 块自动识别，**DST → ffmpeg，其余 → sox_ng**。

> 你机器上 5 个 DFF **全部是 DST 压缩**，所以对你的实际文件，程序会走 ffmpeg 路径。

---

## 最高质量目标是怎么定的（全部来自实测）

**① 位深必须是 24-bit。** 实测 16-bit 的劣化：

| 源 → 目标 | 24-bit | 16-bit | 劣化 |
|---|---|---|---|
| DSD64 → 88.2k | −117.94 dB | −104.39 dB | 13.6 dB |
| DSD128 → 176.4k | −125.45 dB | −107.54 dB | 17.9 dB |
| DSD256 → 352.8k | −133.03 dB | −110.69 dB | 22.3 dB |

**② 目标速率取 2× 基率**：DSD64→**88.2k**、DSD128→**176.4k**、DSD256→**352.8k**；
48k 家族（3.072/6.144/12.288 MHz）自动映射到 96/192/384k。

理由不是噪声底——实测 24-bit 下目标速率对带内噪声底影响很小
（DSD64 在 44.1k–176.4k 间只差 2.6 dB），而且**解码后的音调电平在所有目标速率上完全一致**
（峰值 −12.02 / −12.00 / −11.09 dBFS）。取 2× 是为了**给重建滤镜留过渡带**：
DSD64 转 44.1k 时 20 kHz 距 Nyquist 只剩 2 kHz，滤镜必须陡到会振铃；
转 88.2k 过渡带变成 24 kHz，同样阻带衰减可以用温和得多的滤镜实现。

---

## 界面上的控制

- **位深**：24（默认，推荐）/ 16（弹警告并给出实测劣化数字）
- **目标采样率**：默认按源速率的 2× 自动选中，可改（会提示推荐值）
- **容器**：`wav`（24-bit PCM）或 `flac`（无损压缩，体积约一半）
- **解码后追加效果**：在 `rate` 之后追加 sox 效果，例如 `gain -1`、`sinc 20-38000`
  （DST 走 ffmpeg 时此栏被忽略，程序会提示）
- **一键复制指令** / **直接执行转换** / **验证产出（测带内电平）**

DSD 头信息由纯 Python 解析（不需要 ffprobe），显示容器、真实速率、DSD 等级、压缩类型、声道、时长。

---

## 文件

| 文件 | 作用 |
|---|---|
| `gui.py` | tkinter 界面 |
| `core.py` | 全部逻辑：DSF/DFF 头与 DST 识别、目标速率决策、工具路由、指令拼装、产出测量、剪贴板 |
| `selftest.py` | 自检（24 项）：头解析、目标规则、输出速率必需性、argv 顺序、16-bit 警告、容器、DST 路由、真实执行 |
| `run.bat` | 双击启动 |

```powershell
python selftest.py
```

---

## 已知边界

- **sox_ng 不支持 DST**，DST 文件走 ffmpeg（质量等价，已验证）。
- ffmpeg 无法运⾏ sox 效果，所以 DST 路线下「追加效果」无效。
- 多声道 DSD（>2 声道）通常按声道存成单文件；本程序写单个多声道文件。
- DFF 头不含样本数，时长由文件大小估算；DSF 用头里的精确值。
- 「验证产出」测的是**带内电平**：对音乐文件是节目电平，对静音探针才是噪声底——
  它无法区分两者，所以呈现为电平而非质量结论。
- 输出标签（ID3v2 等）不会被写入，需要另用工具处理。

---

## 附：本机工具位置

| 工具 | 路径 | 用途 |
|---|---|---|
| sox_ng | `%LOCALAPPDATA%\Programs\sox_ng\sox_ng.exe` | DSF / 非压缩 DFF 解码 |
| ffmpeg | `C:\ffmpeg-2025-07-23-git-829680f96a-full_build\bin\ffmpeg.exe` | DST 压缩 DFF 解码 |

官方 SoX 14.4.2 **读不了 DSD**（`no handler for file extension 'dsf'`），本程序不使用它。
