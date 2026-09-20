# SoX PCM → DSD（trellis / sdm）后续工作清单

适用范围：官方 SoX、mansr `sox-dsd` 补丁分支、sox_ng、sox-dsd-win（Windows 预编译）。
本目录另含 `verify-dsd.ps1`：对生成结果做「回解码 + 带内噪声/电平」验证。

---

## 0. 先搞清楚 trellis 在管线里的位置（重要）

SoX 里做 PCM→DSD 的是 **`sdm` 效果**（1-bit sigma-delta 调制器）。
`rate` 只负责升采样；**trellis 是 `sdm` 内部的候选路径搜索**，不是调制之后的独立工序。

因此：

> **不存在「trellis 做完之后再叠一层噪声整形」这一步。**
> 噪声整形（`-f` 选的 clans-N / sdm-N 滤波器）就是调制器的一部分，
> trellis 只是在同一调制器里把量化决策做得更优（加权误差最小化）。

后置工作 = **测量、电平/过载校准、封装、试听**，而不是再串一个整形器。

命令行参数（sox_ng manpage 原文）：

```
sdm [-f filter] [-t order] [-n num] [-l latency]
```

- `-f filter`：噪声整形滤波器 `clans-4|5|6|7|8` 或 `sdm-4|5|6|7|8`（数字=阶数）
  - SoX 里**只有**这两个系列；`crfb-*`、`improved-e-weighted`、`clown`、`none`
    这些名字来自别的工具（Audacity / foo_dsd_trellis 等），**在 SoX 里会报错**。
- partial trellis / Viterbi 搜索（原文：**The noise filter may be combined with a
  partial trellis/viterbi search**）：
  - `-t order`：Trellis order，源码范围 **3–32**
  - `-n num`：Number of trellis paths，源码范围 **4–32**
  - `-l latency`：Output latency，源码范围 **100–2048**
  - **trellis 默认是关的**：不写 `-t/-n/-l` 就没有 trellis；
    开启时若只给部分参数，默认 order **13** / num **8** / latency **1024**。
- ⚠️ **必须显式给 `-f`**：源码里 `sdm_find_filter(NULL, rate)` 对未指定 filter 的情况
  返回该采样率下匹配的**第一个**表项，也就是**最低阶的 clans-4**。
  不写 `-f` 会悄悄退化成 4 阶整形，带内噪声明显变差。
- manpage 明确警告原文：*"The result of using these parameters is hard to predict and
  can include high noise levels or instability. Caution is advised."*
  → trellis 不是「越大越好」，必须靠 §4 的测量来确认，不能只信参数表。
- ⚠️ 社区 wiki 把 `-t 32` 说成「前瞻深度」、`-n 32` 说成「节点数」，与 manpage / 源码的
  `order` / `num` 表述不一致；**以你手上那份 sox 的 `--help-effect sdm` 与源码为准**。
- 若输出位深为 1 bit（`-p 1` 或 DSD 输出格式），SoX 会**自动**调用 `sdm`（默认参数，
  即 clans-4 且无 trellis）；想调参就必须显式写出 `sdm ...`。

### 0.1 源码级事实（`src/sdm.c`，直接决定优化空间）

1. **滤波器表是硬编码的**：整张表只有 `clans-*` / `sdm-*` 各 4 阶，且**每个滤波器都绑定
   一个速率**，只有三档：`64*44100` / `128*44100` / `256*44100`。
   查表是「**freq ≤ 目标速率**的第一个匹配项」。**没有 DSD512 的滤波器**。
   → **你无法自定义噪声整形曲线**，只能用这 18 个预置滤波器。
2. **输入缩放是 `x = sample * 0.5`**（`sdm_process` 里 `x = *ibuf++ * (0.5 / SOX_SAMPLE_MAX)`）。
   即 **PCM 0 dBFS = 调制器满幅 ±1.0 = 0 dBDSD**，而调制器实际稳定上限约 ±0.71。
   → **源素材只要峰值超过 −2.9 dBFS（PCM），就已经越过 SACD 峰值上限**，
   所谓「先 `gain -6`」不是风格选择而是硬性要求。
3. **trellis 的代价函数就是噪声整形的加权误差**：
   `cost += sqr(v ± a[0])`，其中 `a[0]` 是该滤波器的首系数。
   所以 trellis 与噪声整形是同一个优化问题，**不能也不该在之后另加一级整形**。
4. **trellis order 实际是路径历史的哈希位宽**：`mask = (1 << order) - 1`，
   order 越大＝记住越长的输出路径（等价于更深的 Viterbi）。
5. **`clans-*` 与 `sdm-*` 共用同一组 `g[]`（反馈/谐振子系数），只差 `a[]`（前馈/缩放）**，
   且 `sdm-*` 的 `a[0]` 明显更小（1.15 vs 0.74、1.10 vs 0.81 等）。
   trellis 的代价函数用 `a[0]` 加权 → **两种滤波器在 trellis 搜索下的行为不同**，
   「clans 更稳 / sdm 噪声更低」的社区说法与此一致，需实测确认。
6. **trellis 搜索会失败并静默降级**：内部有 `conv_fail` 计数，
   收尾时只打一条 `failed to converge N times` 警告。
   **跑完务必看 stderr**——出现这条说明候选路径塌缩，trellis 没在按你以为的方式工作。

---

## 1. 门槛检查：你手上的 sox 到底支不支持 DSD

官方 SoX v14.4.2（Windows 常见安装包）**完全没有** DSD 能力：

- `sox --help-format all` 里没有 `dsf` / `dff`
- effects 列表里没有 `sdm`、`dsd`、`dop`

```powershell
sox --version
sox --help | Select-String 'sdm|dop'          # 有 sdm 才谈得上 PCM→DSD
sox --help-format all | Select-String 'dsd|dff|wsd'
sox --help-effect sdm                          # 参数表：唯一权威
```

- 没有 → 换 **sox_ng**（当前维护分支，14.8+）或 sox-dsd-win 预编译包。
- 有 → 用 `sox --help-effect sdm` 把参数表抄下来，作为你自己的权威文档。

支持 `sdm` 的构建：`mansr/sox` 的 DSD 补丁系列、**sox_ng**、以及 sox-dsd(-win)。
上游官方 sox 14.4.2 **没有**。

支持 DSD 的输入/输出（sox_ng）：
- `.dff` DSDIFF：可写 DSD64 / 128 / 256
- `.dsf`（DSD Stream File）与 `.wsd`：同样 64 / 128 / 256；`.wsd` 只读不可写
- `-t raw` 裸 1-bit 流（自己封装时用）

DSD→PCM 方向：**没有 `dsd` 效果，但 sox_ng 能正常读 DSD**（我早先的判断有误，已更正）。
它的读取器把 1-bit 展开成 ±满幅样本、速率标为 2.8224 MHz，**PCM 转换交给普通 `rate`**；
SoX 只在**输出速率与输入速率不同**时才自动插入 `rate`，所以**必须给输出采样率**：

```powershell
sox_ng in.dsf -t wav -e signed-integer -b 24 -r 88200 out.wav   # -r 触发自动 rate
sox_ng in.dsf -b 24 out.wav rate -v 88200                       # 或显式 rate
```

不给 `-r` 时输出继承 2.8224 MHz，1-bit 流被当 8-bit PCM 原样写出（看起来像 ±1.0 方波）。
实测加 `-r 88200` 后 sox_ng 与 ffmpeg 的解码结果完全一致（Pk −12.00 dBFS）。
**唯一读不了 DSD 的是官方 SoX 14.4.2**（`no handler for file extension 'dsf'`）；
DSD 读取是 sox_ng 从 14.6.0 起才有的能力。

---

## 2. 调制之前的预处理（顺序别弄反）

1. **位深处理**：源若是 16/24-bit PCM，让 SoX 在内部 32-bit 域处理即可；
   若做了降位，按惯例加 TPDF dither（`dither`），**不要**加噪声整形 dither——
   整形交给 `sdm` 做，两级整形串联只会把带内噪声抬起来。
2. **DC 偏移**：`stats` 看 `DC offset`，非零就用 `highpass 10` 或 `dcshift` 去掉。
   直流会持续吃掉调制器裕量，直接换算成带内噪声和空闲音。
3. **电平校准**：SACD 约定
   - **0 dBDSD = 50% 调制 = PCM −6 dBFS**
   - 峰值上限 **+3.1 dBDSD（≈ PCM −2.9 dBFS）**，超过会调制器过载/失真
   所以从流媒体母带（经常压到 −0.1 dBFS）转 DSD，先 `gain -6` 左右再进 `sdm`，
   而不是「PCM 0 dBFS 对 DSD 满幅」。
4. **升采样到目标 DSD 速率**：44.1k 家族 → DSD64/128/256 是整数比（×64/×128/×256），
   用 `rate -v` 一次升到 2822400 / 5644800 / 11289600。
   48k 家族转 44.1k 家族是非整数比，重采样伪影 + 调制器噪声叠加，需单独测。
5. **避免中途降回低采样率**：`sdm` 的输入必须是已升采样的高采样率信号。

---

## 3. 调制（一次成型）

```bash
# DSD64，clans-8，高质量 trellis（-f 必须显式给，见 §0）
sox input.wav output.dsf rate -v 2822400 sdm -f clans-8 -t 32 -n 32

# 先打通流程：低算力 trellis（不给 -t/-n 就是完全不开 trellis）
sox input.wav output.dsf rate -v 2822400 sdm -f clans-8 -t 8 -n 8

# 只要裸 1-bit 流，自己封装
sox input.wav -t raw -e signed -b 1 -c 2 -r 2822400 out.dsd rate -v 2822400 sdm -f clans-8
```

注：`-t 32 -n 32` 是源码允许的上限（order 3–32、num 4–32），算力开销很大；
先 `-t 8 -n 8` 打通流程与验证，再决定是否升到上限。
只给 `-t` 不给 `-n/-l` 时，其余取默认（order 13 / num 8 / latency 1024）。

阶数选择（越低越稳、越高带内噪声越低但更易过载）：

| 目标 | 常用滤波 |
|---|---|
| DSD64 | clans-8（稳）/ sdm-8（噪声更低但易不稳） |
| DSD128 | clans-7 |
| DSD256 | clans-6 |
| DSD512 | clans-5 |

---

## 4. 调制之后的真正工作

### 4.1 过载 / 不稳定检查（第一优先）
- 看 SoX 是否报 clipping 警告、`-V` 输出里有无异常。
- 裸流统计直流与峰值：解码回 PCM 后 `stats`，DC offset 应≈0。
- **高幅值连续信号**（满幅正弦、粉噪）是调制器最容易崩的用例，必须专门跑。
- manpage 对 trellis 参数本身就有「结果难预测、可能高噪声/不稳定」的警告，
  所以**开 trellis 的结果必须与 `-t 8 -n 8` 甚至无 trellis 的结果做 A/B 测量比对**，
  确认噪声底确实下降而不是变差。

### 4.2 回解码验证（唯一可信的验证方式）
把 DSD 解回 PCM，在 **20 Hz–20 kHz 带内** 看：
- 带内噪声底 vs 理论值（DSD64 + 8 阶约 140 dB 量级动态范围）
- 空闲音（idle tone）：静音输入、−60 dBFS 1 kHz 输入下，带内有无离散尖峰
- THD：−6 dBFS（=0 dBDSD）1 kHz 正弦的谐波

DSD64 的噪声底在 20 kHz 以上会陡升，**测量带宽必须先限带**，否则看到的全是超声噪声。

工作簿：`verify-dsd.ps1`（无 BOM 会乱码，本文件已存为 **UTF-8 with BOM**；
Windows PowerShell 5.1 用 `powershell -File`，装了 PowerShell 7 才用 `pwsh`）
```powershell
# 完整验证：静音带内噪声 + -60dBFS 空闲音 + -6dBFS 参考电平
powershell -File .\verify-dsd.ps1 -Sox <sox.exe> -Input music.wav -WorkDir C:\Harness\sox-dsd\work

# 直接验证一个已存在的 DSD 文件
powershell -File .\verify-dsd.ps1 -Sox <sox.exe> -DsdFile out.dsf
```

### 4.3 封装 / 元数据
> 本节的字节级细节来自通用 DSD 知识与外部工具经验，**未由 sox 源码逐条确证**；
> 以你实际产出的文件头 + 播放器行为为准。
- sox 只写 DSD 容器，**不写标签**。DSF 标签（ID3v2）要另用工具（如 mutagen / Mp3tag），DFF 一般不带标签。
- 多声道：DSF/DFF 写入常只处理立体声；多声道建议按声道拆成单声道 DSD，或自己按
  「每字节含同一声道的 8 个 1-bit 样本、声道逐字节交织」封装。
- 位序：DSF 与 DFF 的字节内位序约定不同，外部工具合流时常见需要 bit-reverse 开关。

### 4.4 播放链路
> 同样属于「按通用 DSD 经验、未在本次源码核对中确证」的部分。
- DoP（`dop` 效果，manpage：*1-bit DSD data is packed into 24-bit samples for
  transport over non-DSD-aware links*）：仅在 176.4/352.8 kHz 等特定速率成立。
- 出问题先查这三件事：**位序、声道交织、速率**。
- DSD64 的超声噪声会激励功放/高音单元互调，必要时播放侧 50 kHz 低通（SACD 规范测量用
  50 kHz Butterworth，30 dB/oct）。

### 4.5 回归与归档
- 固定一条「参考命令 + 版本号」，把每次结果与参考做差分比对；
- 保留测试矩阵：静音 / −60 dBFS / −6 dBFS / 满幅粉噪，四种信号各跑一遍；
- 记录 sox 版本、`sdm` 参数、源文件采样率与峰值电平。

---

## 5. 容易踩的坑

1. 把 trellis 当后处理，调制完再叠 dither/整形 → 带内噪声反而变差。
2. PCM 满幅直入 `sdm` → 超过 +3.1 dBDSD，过载；先 `gain -6`。
3. 在 44.1k **源**速率上调 `sdm`（没有先 `rate -v 2822400`）。
4. 用非 DSD 版的官方 sox 14.4.2 却以为参数写错了。
5. 量测时不限带，把 DSD 的超声噪声当成「底噪很差」。
6. 用 `-t`/`-n` 的社区数值而不核对本机 `--help-effect sdm`（两处文档语义不一致）。
7. 以为 trellis 参数越大越好：manpage 明确说结果难预测、可能高噪声或不稳定。
8. **忘了写 `-f`**：源码会退化成最低阶 clans-4，还看不出报错，只是噪声底变差。
9. 以为 SoX 有 `dsd` 效果或 `crfb-*` / `improved-e-weighted` 之类的滤波器名（那是别的工具）。

---

## 6. 依据来源

- sox_ng manpage（DSD 相关的权威原文）：`sdm` / `dop` 效果条目与 `.dff` / `.dsf` / `.wsd` 格式条目
  — https://man.archlinux.org/man/soxeffect_ng.7.en.txt 、 https://man.archlinux.org/man/soxformat_ng.7.en.txt
- sox_ng 源码 `src/sdm.c`：`-f/-t/-n/-l` 的取值范围、trellis 默认值、
  以及「未指定 filter 时落到 clans-4」这一行为
- sox_ng wiki「DSD Encoding」：各 DSD 速率下的阶数推荐（注意其 `-t/-n` 描述与 manpage 不一致）
  — https://codeberg.org/sox_ng/sox_ng/wiki/DSD-Encoding
