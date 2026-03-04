# StringPulse — 球拍线床状态检测

通过分析敲击拍面的音频，计算弦床振动频率和张力衰减，评估线床状态。

## 工具路径

```bash
PYTHON=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3
SKILL_DIR=/Users/hs/.openclaw/workspace/skills/stringpulse
SCRIPT=$SKILL_DIR/stringpulse.py
```

所有命令从 **`$SKILL_DIR`** 目录执行（确保相对路径正确）：

```bash
cd $SKILL_DIR && $PYTHON stringpulse.py <command> [args]
```

---

## 命令参考

### 列出所有球拍

```bash
$PYTHON stringpulse.py list
```

返回：`{"rackets": [...]}`

### 新建球拍

```bash
$PYTHON stringpulse.py create --name "球拍名称" [--string "球线"] [--tension 55]
```

返回：新建球拍的完整 JSON 对象（含 `id`）

### 删除球拍

```bash
$PYTHON stringpulse.py delete --racket <racket_id>
```

### 分析音频（测量）

```bash
$PYTHON stringpulse.py analyze <音频文件路径> --racket <racket_id> [--filename <原始文件名>] [--date <ISO日期>]
```

- `--filename`：TG 消息里的原始文件名（如 `Jan-23-2026.m4a`），用于从文件名解析日期。**优先级最高**，每次调用都应传入（从 `message.document.file_name` 获取）。
- `--date`：TG 消息发送时间（ISO 格式），作为文件名无日期时的回退。

返回：

```json
{
  "id": "...",
  "date": "...",
  "frequency": 562.3,
  "loss": 8.4,
  "ra": 91.6,
  "status": "optimal",
  "pulses": [...],
  "saved": true,
  "image_path": "/path/to/data/images/xxx.png"
}
```

若无基准频率，`loss` 和 `ra` 为 `null`，`status` 为 `"no_baseline"`。

### 设置基准频率（首次录音，独立流程）

```bash
$PYTHON stringpulse.py baseline <音频文件路径> --racket <racket_id> [--date <ISO日期>]
```

直接分析音频并设为基准频率（不询问用户，适合已知需要设基准的场景）。
返回格式同 `analyze`，`status` 为 `"baseline_set"`。

### 升级已有测量为基准频率

```bash
$PYTHON stringpulse.py promote-baseline --racket <racket_id> --measurement <measurement_id>
```

将 `analyze` 已保存的记录原地升级为基准频率，**不新增测量记录**。
返回：`{"id", "date", "frequency", "loss": 0.0, "ra": 100.0, "status": "baseline_set", "image_path"}`

---

## 业务流程

### 流程 A：新建球拍

```
用户："新建球拍" / "我买了新拍" / "添加球拍"
  → 询问：球拍型号？
  → 询问：球线品牌和型号？（可选，用户说"跳过"则不传）
  → 询问：穿线磅数？（可选）
  → 执行 create 命令
  → 告知用户球拍已记录，建议穿线后发音频设置基准频率
```

### 流程 B：测量线床状态

```
用户："检测拍线" / "测量球拍" / "看看线床状态"
  → 执行 list，展示球拍列表，让用户选一只
  → 提示用户录制音频（见下方提示语）
  → 用户发送文件 → 获取 MediaPath
  → 执行 analyze <MediaPath> --racket <id> --filename <document.file_name> --date <TG消息发送时间ISO>
  → 发送结果图片（image_path 字段），再用 1-2 句总结
```

**告知用户录制方式的标准提示语**：

> 请用 iPhone「语音备忘录」录制：手持球拍，用手指用力弹拨拍弦 3-5 次，每次间隔约 1 秒。录制完成后：
> ⚠️ **在 Telegram 里请选「附件 → 文件」发送，不要点「语音消息」按钮。**
> （语音消息会被转成文字，无法分析音频。）

### 流程 C：首次测量（无基准）

```
analyze 返回 loss=null / status="no_baseline" 时：
  → 告知用户："这只球拍还没有基准频率。"
  → 问："是否将这次测量（XXX Hz）设为基准？"
  → 若是：执行 promote-baseline --racket <id> --measurement <analyze返回的id>
  → 发送结果图片并说明已设为基准
```

⚠️ **不要**再次执行 `baseline <音频路径>`，否则会产生重复记录。
`promote-baseline` 直接升级已有记录，不新增测量。

---

## 结果解读

| 状态 | 条件 | 含义 |
|------|------|------|
| `optimal` | loss ≤ 10% | 状态最佳，线床张力保持良好 |
| `mild_fatigue` | 10% < loss ≤ 25% | 轻度疲劳，可继续使用但留意 |
| `restring` | loss > 25% | 建议换线，张力已大幅衰减 |
| `no_baseline` | 无基准 | 暂无基准，无法计算 Loss |
| `baseline_set` | 刚设基准 | 基准频率已记录 |

---

## 重要注意事项

### ⚠️ 音频文件 ≠ 语音消息

- 用户发来的是拍线敲击**声学数据**，不是语音转文字的内容
- **绝对不要**使用 STT transcript，必须使用 `{{MediaPath}}` 获取文件路径
- 每次要求用户发音频时，都必须提醒选「附件 → 文件」

### 📊 结果图片发送

- `analyze` 和 `baseline` 命令执行成功后，返回 JSON 含 `image_path`
- 先通过 Telegram 发送该图片给用户
- 再用 1-2 句话总结：当前频率、Loss%、状态建议

### 🔧 错误处理

- 若命令返回 `{"error": "..."}` 且退出码非零，说明出错
- 常见错误："未找到足够的有效脉冲" → 音频质量不佳，请用户重新录制
- ffmpeg 未安装 → 提示用户安装：`brew install ffmpeg`
- matplotlib 未安装 → `pip3 install matplotlib`

---

## 技术说明（供调试参考）

- **记录日期优先级**：① `--filename` 原始文件名含日期 → 取文件名日期；② `--date` → 取 TG 消息发送时间；③ 两者均无 → 取当前系统时间
  - 支持纯数字：`20260304`、`2026-03-04`、`20260304_143000`
  - 支持英文月份（缩写或全称，大小写不限）：`Jan-23-2026`、`Mar-4-2026`、`4-Mar-2026`、`March 4 2026`
- **音频格式**：支持 M4A/AAC/WAV/MP4 等，通过 ffmpeg 解码为 PCM float32
- **FFT 算法**：Cooley-Tukey，窗口大小最大 8192 样本
- **频率范围**：400–800 Hz（网球拍弦床振动频率范围）
- **脉冲检测**：将音频分为 10ms RMS 窗口，RMS 上升沿超过峰值 RMS 的 20% 时视为脉冲起始点，最小间隔 0.5s（与 JS 版一致，抗噪优于逐样本检测）
- **Loss/RA 公式**：`Loss = (1 - f_cur² / f_base²) × 100`，`RA = f_cur / f_base × 100`（二次能量公式，弦张力 T ∝ f²，与 JS 版一致）
- **异常值剔除**：移除偏离中位数超过 15% 的脉冲频率
- **数据存储**：`$SKILL_DIR/data/rackets.json`（自动创建）
