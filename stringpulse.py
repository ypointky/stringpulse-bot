#!/usr/bin/env python3
"""
StringPulse — 球拍线床状态检测 CLI
用法: python3 stringpulse.py <command> [args]
"""

import argparse
import json
import math
import os
import re
import struct
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ─── 常量配置（与 JS 版对应）───────────────────────────────────────────
THRESHOLD_RATIO    = 0.20   # 脉冲检测阈值（峰值的 20%）
MIN_PULSE_INTERVAL = 0.5    # 最小脉冲间隔（秒）
PULSE_DURATION     = 0.5    # 每次脉冲分析时长（秒）
FFT_MAX            = 8192   # FFT 最大窗口
FREQ_MIN           = 400    # 最小频率 Hz
FREQ_MAX           = 800    # 最大频率 Hz
SNR_MIN            = 3.0    # 最低信噪比
OUTLIER_PCT        = 0.15   # 异常值偏离阈值（15%）

# ─── 路径配置 ──────────────────────────────────────────────────────────
SKILL_DIR  = Path(__file__).parent
DATA_DIR   = SKILL_DIR / "data"
IMAGES_DIR = DATA_DIR / "images"
DATA_FILE  = DATA_DIR / "rackets.json"


# ══════════════════════════════════════════════════════════════════════
# 数据读写
# ══════════════════════════════════════════════════════════════════════

def _load_data():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        DATA_FILE.write_text(json.dumps({"rackets": []}, indent=2))
    return json.loads(DATA_FILE.read_text())


def _save_data(data):
    DATA_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _find_racket(data, racket_id):
    for r in data["rackets"]:
        if r["id"] == racket_id:
            return r
    return None


# ══════════════════════════════════════════════════════════════════════
# FFT — Cooley-Tukey（移植自 JS 版）
# ══════════════════════════════════════════════════════════════════════

def _fft(re, im):
    n = len(re)
    # Bit-reversal permutation
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            re[i], re[j] = re[j], re[i]
            im[i], im[j] = im[j], im[i]
    # Butterfly stages
    length = 2
    while length <= n:
        half = length >> 1
        ang = -math.pi / half
        w_re0 = math.cos(ang)
        w_im0 = math.sin(ang)
        for i in range(0, n, length):
            w_re, w_im = 1.0, 0.0
            for k in range(half):
                u_re = re[i + k]
                u_im = im[i + k]
                v_re = re[i + k + half] * w_re - im[i + k + half] * w_im
                v_im = re[i + k + half] * w_im + im[i + k + half] * w_re
                re[i + k]          = u_re + v_re
                im[i + k]          = u_im + v_im
                re[i + k + half]   = u_re - v_re
                im[i + k + half]   = u_im - v_im
                new_w_re = w_re * w_re0 - w_im * w_im0
                w_im     = w_re * w_im0 + w_im * w_re0
                w_re     = new_w_re
        length <<= 1


# ══════════════════════════════════════════════════════════════════════
# 音频解码（ffmpeg → PCM float32）
# ══════════════════════════════════════════════════════════════════════

def decode_audio(path):
    tmp = "/tmp/sp_raw.f32"
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", path,
         "-ar", "44100", "-ac", "1", "-f", "f32le", tmp],
        capture_output=True
    )
    if result.returncode != 0:
        err = result.stderr.decode(errors="replace")
        raise RuntimeError(f"ffmpeg 解码失败：{err[-300:]}")
    raw = open(tmp, "rb").read()
    os.unlink(tmp)
    n = len(raw) // 4
    if n == 0:
        raise RuntimeError("音频文件为空或无法解码")
    samples = list(struct.unpack(f"{n}f", raw))
    return samples, 44100


# ══════════════════════════════════════════════════════════════════════
# 脉冲检测（移植自 JS _detectPulses）
# ══════════════════════════════════════════════════════════════════════

def _detect_pulses(samples, sample_rate):
    WINDOW_MS = 10
    win_size = int(sample_rate * WINDOW_MS / 1000)
    if win_size == 0:
        return []
    n_win = len(samples) // win_size

    rms = []
    for w in range(n_win):
        base = w * win_size
        s = sum(samples[base + i] ** 2 for i in range(win_size))
        rms.append(math.sqrt(s / win_size))

    peak = max(rms) if rms else 0
    if peak == 0:
        return []
    threshold = peak * THRESHOLD_RATIO

    pulses = []
    min_win = math.ceil(MIN_PULSE_INTERVAL * sample_rate / win_size)
    last_w = -min_win

    for w in range(1, n_win):
        if rms[w] >= threshold and rms[w - 1] < threshold and (w - last_w) >= min_win:
            start = w * win_size
            time_s = start / sample_rate
            pulses.append({"start": start, "timeS": time_s})
            last_w = w
    return pulses


# ══════════════════════════════════════════════════════════════════════
# FFT 分析单脉冲（移植自 JS _analyzeFFT）
# ══════════════════════════════════════════════════════════════════════

def _analyze_fft(samples, start, sample_rate):
    duration_samples = int(PULSE_DURATION * sample_rate)
    end = min(start + duration_samples, len(samples))
    segment = samples[start:end]

    # 对齐到 2 的幂
    n = 1
    while n < len(segment):
        n <<= 1
    n = min(n, FFT_MAX)
    if n > len(segment):
        n >>= 1
    if n < 16:
        return None

    # Hann 窗
    re = [segment[i] * (0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)))
          for i in range(n)]
    im = [0.0] * n

    _fft(re, im)

    # 频率分辨率
    freq_res = sample_rate / n
    bin_min = max(1, int(FREQ_MIN / freq_res))
    bin_max = min(n // 2, int(FREQ_MAX / freq_res) + 1)

    # 找峰值 bin
    magnitudes = [math.sqrt(re[k] ** 2 + im[k] ** 2) for k in range(n)]
    peak_bin = max(range(bin_min, bin_max), key=lambda k: magnitudes[k])
    peak_mag = magnitudes[peak_bin]

    # 噪底（排除峰值附近 ±5 bins）
    noise_bins = [magnitudes[k] for k in range(bin_min, bin_max)
                  if abs(k - peak_bin) > 5]
    if not noise_bins:
        return None
    noise_floor = sum(noise_bins) / len(noise_bins)
    if noise_floor == 0:
        return None
    snr = peak_mag / noise_floor

    if snr < SNR_MIN:
        return None

    # 抛物线插值精确峰值
    if 0 < peak_bin < n - 1:
        alpha = magnitudes[peak_bin - 1]
        beta  = magnitudes[peak_bin]
        gamma = magnitudes[peak_bin + 1]
        denom = alpha - 2 * beta + gamma
        offset = (alpha - gamma) / (2 * denom) if denom != 0 else 0
    else:
        offset = 0

    freq = (peak_bin + offset) * freq_res
    return freq


# ══════════════════════════════════════════════════════════════════════
# 异常值剔除 + 统计
# ══════════════════════════════════════════════════════════════════════

def _median(values):
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _process_pulses(raw_pulses):
    """raw_pulses: list of {"timeS": ..., "freq": ...}"""
    if not raw_pulses:
        return []
    freqs = [p["freq"] for p in raw_pulses]
    med = _median(freqs)
    result = []
    for p in raw_pulses:
        is_outlier = abs(p["freq"] - med) / med > OUTLIER_PCT
        result.append({
            "timeS": round(p["timeS"], 3),
            "freq": round(p["freq"], 2),
            "isOutlier": is_outlier
        })
    return result


def _mean_frequency(pulses):
    valid = [p["freq"] for p in pulses if not p["isOutlier"]]
    if not valid:
        return None
    return sum(valid) / len(valid)


# ══════════════════════════════════════════════════════════════════════
# 完整音频分析
# ══════════════════════════════════════════════════════════════════════

def analyze_audio(audio_path):
    samples, sr = decode_audio(audio_path)
    raw_pulse_starts = _detect_pulses(samples, sr)
    if not raw_pulse_starts:
        raise RuntimeError("未检测到有效的敲击脉冲，请确认音频包含敲击拍面的声音")

    raw_pulses = []
    for p in raw_pulse_starts:
        freq = _analyze_fft(samples, p["start"], sr)
        if freq is not None:
            raw_pulses.append({"timeS": p["timeS"], "freq": freq})

    pulses = _process_pulses(raw_pulses)
    mean_freq = _mean_frequency(pulses)

    if mean_freq is None:
        raise RuntimeError("未找到足够的有效脉冲，请重新录制（每次弹拨间隔约 1 秒，共 3-5 次）")

    return pulses, mean_freq


# ══════════════════════════════════════════════════════════════════════
# 状态计算
# ══════════════════════════════════════════════════════════════════════

def _calc_status(loss):
    if loss is None:
        return "no_baseline"
    if loss <= 10:
        return "optimal"
    if loss <= 25:
        return "mild_fatigue"
    return "restring"


STATUS_LABELS = {
    "optimal":       "状态最佳",
    "mild_fatigue":  "轻度疲劳",
    "restring":      "建议换线",
    "no_baseline":   "暂无基准",
    "baseline_set":  "已设基准",
}

STATUS_COLORS = {
    "optimal":      "#34c759",
    "mild_fatigue": "#ff9500",
    "restring":     "#ff3b30",
    "no_baseline":  "#6b7280",
    "baseline_set": "#34c759",
}

STATUS_ADVICE = {
    "optimal":      "线床状态良好，继续保持",
    "mild_fatigue": "张力有所衰减，可继续使用，注意下次测量",
    "restring":     "张力衰减明显，建议尽快换线",
    "no_baseline":  "暂无基准频率，无法评估衰减",
    "baseline_set": "基准频率已记录，之后测量将以此为参照",
}


# ══════════════════════════════════════════════════════════════════════
# 图片生成（matplotlib）
# ══════════════════════════════════════════════════════════════════════

def generate_result_image(racket, measurement, status, output_path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        matplotlib.rcParams["font.family"] = ["Heiti TC", "Arial Unicode MS", "DejaVu Sans"]
        matplotlib.rcParams["axes.unicode_minus"] = False
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except ImportError:
        return None

    color  = STATUS_COLORS.get(status, "#6b7280")
    label  = STATUS_LABELS.get(status, status)
    advice = STATUS_ADVICE.get(status, "")

    freq          = measurement.get("frequency")
    loss          = measurement.get("loss")
    ra            = measurement.get("ra")
    meas_date     = measurement.get("date", "")[:10]
    baseline_freq = (racket.get("baseline") or {}).get("frequency")
    tension       = racket.get("tension")
    string_name   = racket.get("string") or ""
    racket_name   = racket.get("name", "")
    cur_id        = measurement.get("id")

    # All measurements sorted ascending by date
    all_m     = sorted(racket.get("measurements", []), key=lambda m: m.get("date", ""))
    show_hist = all_m[-8:]   # at most 8 most recent
    n_hist    = len(show_hist)

    # ── Section heights (inches) ────────────────────────────────
    HDR_H    = 0.55   # header bar
    DASH_H   = 3.00   # ring + 2x2 metrics
    STAT_H   = 0.58   # status badge + advice
    GAP      = 0.20   # gap between cards
    CHDR_H   = 0.30   # section title height
    CHART_H  = 2.40   # frequency trend chart
    HIST_ROW = 0.44   # height per history row
    HIST_GAP = 0.06   # gap between history rows
    BOT_PAD  = 0.25

    hist_block_h = CHDR_H + n_hist * (HIST_ROW + HIST_GAP)
    total_h = HDR_H + GAP + DASH_H + STAT_H + GAP + CHDR_H + CHART_H + GAP + hist_block_h + BOT_PAD

    fig = plt.figure(figsize=(8.0, total_h), facecolor="#f5f7fa")

    def ay(y_top, h):
        """(y from top in inches, height in inches) → (fig bottom frac, fig height frac)"""
        return 1.0 - (y_top + h) / total_h, h / total_h

    LM = 0.035   # left margin (figure fraction)
    RM = 0.965
    CW = RM - LM

    # ── 1. HEADER BAR ───────────────────────────────────────────
    b, h = ay(0, HDR_H)
    ax_hdr = fig.add_axes([0, b, 1, h], facecolor="#007AFF")
    ax_hdr.axis("off")
    parts = [racket_name]
    if string_name: parts.append(string_name)
    if tension:     parts.append(f"{tension}磅")
    ax_hdr.text(0.03, 0.70, "StringPulse", fontsize=12, fontweight="bold",
                color="white", va="center", transform=ax_hdr.transAxes)
    ax_hdr.text(0.03, 0.22, "  ·  ".join(parts), fontsize=9.5, color="#cce4ff",
                va="center", transform=ax_hdr.transAxes)
    ax_hdr.text(0.97, 0.50, meas_date, fontsize=10, color="white",
                ha="right", va="center", transform=ax_hdr.transAxes)

    # ── 2. DASHBOARD: RA RING ───────────────────────────────────
    y_dash = HDR_H + GAP
    ring_col_w = CW * 0.36
    ring_pad_t = 0.14
    ring_pad_b = 0.20

    b, h = ay(y_dash + ring_pad_t, DASH_H - ring_pad_t - ring_pad_b)
    ax_ring = fig.add_axes([LM, b, ring_col_w, h], facecolor="none")
    ax_ring.set_aspect("equal", adjustable="box")
    ax_ring.axis("off")
    ax_ring.set_xlim(0, 1)
    ax_ring.set_ylim(0, 1)

    ax_ring.add_patch(mpatches.Arc((0.5, 0.5), 0.72, 0.72, angle=0,
                                   theta1=0, theta2=360,
                                   color="#e5e7eb", linewidth=14))
    if ra is not None and ra > 0:
        sweep = ra / 100.0 * 360.0
        ax_ring.add_patch(mpatches.Arc((0.5, 0.5), 0.72, 0.72, angle=0,
                                       theta1=90 - sweep, theta2=90,
                                       color=color, linewidth=14,
                                       capstyle="round"))
        ax_ring.text(0.5, 0.56, f"{ra:.1f}", fontsize=22, fontweight="bold",
                     ha="center", va="center", color=color)
        ax_ring.text(0.5, 0.37, "RA 指数", fontsize=8.5,
                     ha="center", va="center", color="#6b7280")
    else:
        ax_ring.text(0.5, 0.52, "—", fontsize=28, fontweight="bold",
                     ha="center", va="center", color="#6b7280")
        ax_ring.text(0.5, 0.33, "RA 指数", fontsize=8.5,
                     ha="center", va="center", color="#6b7280")

    # ── 3. DASHBOARD: 2×2 METRICS ───────────────────────────────
    met_l  = LM + ring_col_w + 0.018
    met_cw = (RM - met_l - 0.012) / 2
    met_gap_x = 0.012
    met_pad_t = 0.16
    met_pad_b = 0.18
    met_gap_y = 0.12
    met_row_h = (DASH_H - met_pad_t - met_pad_b - met_gap_y) / 2

    metrics = [
        ("当前频率 (Hz)", f"{freq:.1f}" if freq is not None else "—",         "#1a1a2e"),
        ("衰减 Loss%",    f"{loss:.1f}%" if loss is not None else "—",         color if loss is not None else "#1a1a2e"),
        ("基准频率 (Hz)", f"{baseline_freq:.1f}" if baseline_freq is not None else "—", "#1a1a2e"),
        ("最近测量",      meas_date,                                            "#1a1a2e"),
    ]
    for i, (mlabel, mval, mcolor) in enumerate(metrics):
        col = i % 2
        row = i // 2
        mx     = met_l + col * (met_cw + met_gap_x)
        my_top = y_dash + met_pad_t + row * (met_row_h + met_gap_y)
        b, h   = ay(my_top, met_row_h)
        ax_m   = fig.add_axes([mx, b, met_cw, h], facecolor="#f9fafb")
        for sp in ax_m.spines.values():
            sp.set_edgecolor("#e5e7eb")
            sp.set_linewidth(0.8)
        ax_m.tick_params(left=False, bottom=False, labelleft=False, labelbottom=False)
        ax_m.text(0.5, 0.72, mlabel, fontsize=8,   ha="center", va="center",
                  color="#6b7280", transform=ax_m.transAxes)
        ax_m.text(0.5, 0.28, mval,   fontsize=14,  ha="center", va="center",
                  fontweight="bold", color=mcolor, transform=ax_m.transAxes)

    # ── 4. STATUS BADGE + ADVICE ─────────────────────────────────
    b, h = ay(y_dash + DASH_H, STAT_H)
    ax_st = fig.add_axes([LM, b, CW, h], facecolor=color + "22")
    for sp in ax_st.spines.values():
        sp.set_edgecolor(color)
        sp.set_linewidth(1.5)
    ax_st.tick_params(left=False, bottom=False, labelleft=False, labelbottom=False)
    ax_st.text(0.5, 0.70, label, fontsize=11, fontweight="bold",
               ha="center", va="center", color=color, transform=ax_st.transAxes)
    ax_st.text(0.5, 0.24, f"建议：{advice}", fontsize=9,
               ha="center", va="center", color="#374151", transform=ax_st.transAxes)

    # ── 5. FREQUENCY TREND CHART ─────────────────────────────────
    y_chart_sec = y_dash + DASH_H + STAT_H + GAP
    fig.text(LM, 1.0 - y_chart_sec / total_h,
             "频率趋势", fontsize=10, fontweight="bold", color="#1a1a2e", va="bottom")

    b, h = ay(y_chart_sec + CHDR_H, CHART_H)
    ax_c = fig.add_axes([LM, b, CW, h], facecolor="white")

    chart_dates  = [m.get("date", "")[:10] for m in show_hist]
    chart_freqs  = [m.get("frequency") for m in show_hist]
    chart_losses = [m.get("loss") for m in show_hist]
    x_pos        = list(range(len(show_hist)))
    x_labels     = [d[5:].replace("-", "/") for d in chart_dates]

    if baseline_freq is not None:
        ax_c.axhline(y=baseline_freq, color="#9ca3af", linewidth=1.2,
                     linestyle="--", alpha=0.8, zorder=1,
                     label=f"基准 {baseline_freq:.1f} Hz")

    if len(x_pos) >= 2:
        ax_c.plot(x_pos, chart_freqs, color="#007AFF", linewidth=2,
                  alpha=0.7, zorder=2)

    for xi, yf, ml in zip(x_pos, chart_freqs, chart_losses):
        if yf is None:
            continue
        pt_c = ("#6b7280" if ml is None else
                "#34c759" if ml <= 10 else
                "#ff9500" if ml <= 25 else "#ff3b30")
        ax_c.scatter([xi], [yf], color=pt_c, s=60, zorder=4,
                     edgecolors="white", linewidths=1.5)
        ax_c.annotate(f"{yf:.1f}", (xi, yf),
                      textcoords="offset points", xytext=(0, 8),
                      ha="center", fontsize=7.5, color="#374151")

    # Highlight current measurement with larger marker
    for xi, m in enumerate(show_hist):
        if m.get("id") == cur_id and chart_freqs[xi] is not None:
            ax_c.scatter([xi], [chart_freqs[xi]], color=color, s=110, zorder=5,
                         edgecolors="white", linewidths=2)

    ax_c.set_xticks(x_pos)
    ax_c.set_xticklabels(x_labels, fontsize=8.5, color="#374151")
    ax_c.tick_params(axis="x", bottom=False)
    ax_c.tick_params(axis="y", labelsize=8, labelcolor="#6b7280")
    ax_c.set_ylabel("Hz", fontsize=8, color="#6b7280", labelpad=4)
    for sp in ["top", "right"]:
        ax_c.spines[sp].set_visible(False)
    ax_c.spines["left"].set_color("#e5e7eb")
    ax_c.spines["bottom"].set_color("#e5e7eb")
    ax_c.grid(axis="y", color="#f3f4f6", linewidth=0.8, zorder=0)

    valid_freqs = [f for f in chart_freqs if f is not None]
    if valid_freqs:
        ymin = min(valid_freqs + ([baseline_freq] if baseline_freq else []))
        ymax = max(valid_freqs + ([baseline_freq] if baseline_freq else []))
        pad  = max((ymax - ymin) * 0.30, 15)
        ax_c.set_ylim(ymin - pad, ymax + pad)

    if baseline_freq is not None:
        ax_c.legend(fontsize=8, loc="lower left",
                    framealpha=0.9, edgecolor="#e5e7eb", fancybox=False)

    # ── 6. MEASUREMENT HISTORY LIST ──────────────────────────────
    y_hist_sec = y_chart_sec + CHDR_H + CHART_H + GAP
    fig.text(LM, 1.0 - y_hist_sec / total_h,
             "测量记录", fontsize=10, fontweight="bold", color="#1a1a2e", va="bottom")

    for idx, m in enumerate(reversed(show_hist)):   # most recent first
        m_date   = m.get("date", "")[:10]
        m_freq   = m.get("frequency")
        m_loss   = m.get("loss")
        n_valid  = len([p for p in m.get("pulses", []) if not p.get("isOutlier", False)])
        is_cur   = m.get("id") == cur_id

        row_top  = y_hist_sec + CHDR_H + idx * (HIST_ROW + HIST_GAP)
        b, h     = ay(row_top, HIST_ROW)
        ax_r     = fig.add_axes([LM, b, CW, h],
                                facecolor="#f0f7ff" if is_cur else "#f9fafb")
        for sp in ax_r.spines.values():
            sp.set_edgecolor("#007AFF" if is_cur else "#e5e7eb")
            sp.set_linewidth(1.2 if is_cur else 0.8)
        ax_r.axis("off")

        sub = f"{n_valid} 有效脉冲" if n_valid > 0 else "手动录入"
        ax_r.text(0.02, 0.65, m_date, fontsize=10, fontweight="500",
                  color="#1a1a2e", va="center", transform=ax_r.transAxes)
        ax_r.text(0.02, 0.22, sub, fontsize=8, color="#6b7280",
                  va="center", transform=ax_r.transAxes)

        if m_freq is not None:
            ax_r.text(0.62, 0.50, f"{m_freq:.1f} Hz", fontsize=12,
                      fontweight="bold", ha="right", va="center",
                      color="#1a1a2e", transform=ax_r.transAxes)

        if m_loss is not None:
            loss_c = ("#34c759" if m_loss <= 10 else
                      "#ff9500" if m_loss <= 25 else "#ff3b30")
            ax_r.text(0.98, 0.50, f"Loss {m_loss:.1f}%", fontsize=11,
                      fontweight="bold", ha="right", va="center",
                      color=loss_c, transform=ax_r.transAxes)
        else:
            ax_r.text(0.98, 0.50, "基准", fontsize=10,
                      fontweight="bold", ha="right", va="center",
                      color="#34c759", transform=ax_r.transAxes)

    # ── Save ─────────────────────────────────────────────────────
    fig.savefig(output_path, dpi=150, bbox_inches="tight", facecolor="#f5f7fa")
    plt.close(fig)
    return str(output_path)


# ══════════════════════════════════════════════════════════════════════
# CLI 命令实现
# ══════════════════════════════════════════════════════════════════════

def cmd_list(args):
    data = _load_data()
    out = []
    for r in data["rackets"]:
        out.append({
            "id":      r["id"],
            "name":    r["name"],
            "string":  r.get("string"),
            "tension": r.get("tension"),
            "baseline_freq": r["baseline"]["frequency"] if r.get("baseline") else None,
            "measurement_count": len(r.get("measurements", [])),
        })
    print(json.dumps({"rackets": out}, ensure_ascii=False, indent=2))


def cmd_create(args):
    data = _load_data()
    racket = {
        "id":           str(uuid.uuid4()),
        "name":         args.name,
        "string":       args.string,
        "tension":      args.tension,
        "baseline":     None,
        "measurements": [],
    }
    data["rackets"].append(racket)
    _save_data(data)
    print(json.dumps(racket, ensure_ascii=False, indent=2))


def cmd_delete(args):
    data = _load_data()
    before = len(data["rackets"])
    data["rackets"] = [r for r in data["rackets"] if r["id"] != args.racket]
    if len(data["rackets"]) == before:
        _error(f"未找到球拍 ID: {args.racket}")
    _save_data(data)
    print(json.dumps({"deleted": args.racket}))


def _do_analyze(audio_path, racket_id, set_as_baseline=False, date_str=None):
    data  = _load_data()
    racket = _find_racket(data, racket_id)
    if racket is None:
        _error(f"未找到球拍 ID: {racket_id}")

    pulses, mean_freq = analyze_audio(audio_path)

    baseline = racket.get("baseline")
    if baseline and not set_as_baseline:
        base_freq = baseline["frequency"]
        loss = round((1 - (mean_freq ** 2) / (base_freq ** 2)) * 100, 2)
        ra   = round((mean_freq / base_freq) * 100, 2)
        status = _calc_status(loss)
    else:
        loss = None
        ra   = None
        status = "baseline_set" if set_as_baseline else "no_baseline"

    now = (_parse_date_from_filename(audio_path)
           or date_str
           or datetime.now(timezone.utc).isoformat())
    measurement_id = str(uuid.uuid4())

    measurement = {
        "id":        measurement_id,
        "date":      now,
        "frequency": round(mean_freq, 2),
        "loss":      loss,
        "ra":        ra,
        "pulses":    pulses,
    }

    if set_as_baseline:
        racket["baseline"] = {
            "frequency": round(mean_freq, 2),
            "date":      now,
        }

    racket.setdefault("measurements", []).append(measurement)
    _save_data(data)

    # 生成结果图片
    image_path = IMAGES_DIR / f"{measurement_id}.png"
    img = generate_result_image(racket, measurement, status, image_path)

    result = {
        "id":         measurement_id,
        "date":       now,
        "frequency":  round(mean_freq, 2),
        "loss":       loss,
        "ra":         ra,
        "status":     status,
        "pulses":     pulses,
        "saved":      True,
        "image_path": img,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_analyze(args):
    if not os.path.exists(args.audio):
        _error(f"音频文件不存在: {args.audio}")
    _do_analyze(args.audio, args.racket, set_as_baseline=False, date_str=args.date)


def cmd_baseline(args):
    if not os.path.exists(args.audio):
        _error(f"音频文件不存在: {args.audio}")
    _do_analyze(args.audio, args.racket, set_as_baseline=True, date_str=args.date)


# ══════════════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════════════

def _parse_date_from_filename(audio_path):
    """从音频文件名中提取日期，返回 ISO 字符串；识别失败则返回 None。"""
    stem = Path(audio_path).stem

    # ── 1. 纯数字格式 ──────────────────────────────────────────────────
    num_patterns = [
        # YYYY-MM-DD_HH-MM-SS / YYYY.MM.DD HH:MM:SS 等（带时间）
        (r'(\d{4})[-_./](\d{2})[-_./](\d{2})[_ T](\d{2})[-_:.](\d{2})[-_:.](\d{2})', 6),
        # YYYYMMDD_HHMMSS（无分隔符）
        (r'(\d{4})(\d{2})(\d{2})[_ T](\d{2})(\d{2})(\d{2})', 6),
        # YYYY-MM-DD / YYYY_MM_DD / YYYY.MM.DD（仅日期）
        (r'(\d{4})[-_.](\d{2})[-_.](\d{2})', 3),
        # YYYYMMDD（纯 8 位，不紧邻其他数字）
        (r'(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)', 3),
    ]
    for pat, n in num_patterns:
        m = re.search(pat, stem)
        if not m:
            continue
        g = m.groups()
        try:
            if n == 6:
                dt = datetime(int(g[0]), int(g[1]), int(g[2]),
                              int(g[3]), int(g[4]), int(g[5]),
                              tzinfo=timezone.utc)
            else:
                dt = datetime(int(g[0]), int(g[1]), int(g[2]),
                              tzinfo=timezone.utc)
            if 2000 <= dt.year <= 2100:
                return dt.isoformat()
        except ValueError:
            continue

    # ── 2. 含英文月份名的格式（缩写或全称，大小写不限）────────────────
    # 支持：Mar-4-2026 / 4-Mar-2026 / March 4 2026 / 4 March 2026 等
    MONTH_MAP = {
        'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
        'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
        'january': 1, 'february': 2, 'march': 3, 'april': 4,
        'june': 6, 'july': 7, 'august': 8, 'september': 9,
        'october': 10, 'november': 11, 'december': 12,
    }
    SEP = r'[-_ .]'
    MON = (r'(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?'
           r'|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?'
           r'|Nov(?:ember)?|Dec(?:ember)?)')
    mon_patterns = [
        (rf'{MON}{SEP}(\d{{1,2}}){SEP}(\d{{4}})', 'mdy'),  # Mar-4-2026
        (rf'(\d{{1,2}}){SEP}{MON}{SEP}(\d{{4}})', 'dmy'),  # 4-Mar-2026
    ]
    for pat, order in mon_patterns:
        m = re.search(pat, stem, re.IGNORECASE)
        if not m:
            continue
        g = m.groups()
        try:
            if order == 'mdy':
                mon_str, day, year = g[0], int(g[1]), int(g[2])
            else:
                day, mon_str, year = int(g[0]), g[1], int(g[2])
            month = MONTH_MAP.get(mon_str.lower())
            if month is None:
                continue
            dt = datetime(year, month, day, tzinfo=timezone.utc)
            if 2000 <= dt.year <= 2100:
                return dt.isoformat()
        except ValueError:
            continue

    return None


def _error(msg):
    print(json.dumps({"error": msg}, ensure_ascii=False), file=sys.stderr)
    sys.exit(1)


# ══════════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="StringPulse — 球拍线床状态检测"
    )
    sub = parser.add_subparsers(dest="command")

    # list
    sub.add_parser("list", help="列出所有球拍")

    # create
    p_create = sub.add_parser("create", help="新建球拍")
    p_create.add_argument("--name",    required=True, help="球拍名称")
    p_create.add_argument("--string",  default=None,  help="球线品牌和型号")
    p_create.add_argument("--tension", type=float, default=None, help="穿线磅数")

    # delete
    p_del = sub.add_parser("delete", help="删除球拍")
    p_del.add_argument("--racket", required=True, help="球拍 ID")

    # analyze
    p_ana = sub.add_parser("analyze", help="分析音频，保存测量记录")
    p_ana.add_argument("audio",    help="音频文件路径")
    p_ana.add_argument("--racket", required=True, help="球拍 ID")
    p_ana.add_argument("--date",   default=None,  help="录制日期（TG 消息发送时间，ISO 格式）")

    # baseline
    p_bas = sub.add_parser("baseline", help="分析音频，设为基准频率")
    p_bas.add_argument("audio",    help="音频文件路径")
    p_bas.add_argument("--racket", required=True, help="球拍 ID")
    p_bas.add_argument("--date",   default=None,  help="录制日期（TG 消息发送时间，ISO 格式）")

    args = parser.parse_args()

    try:
        if args.command == "list":
            cmd_list(args)
        elif args.command == "create":
            cmd_create(args)
        elif args.command == "delete":
            cmd_delete(args)
        elif args.command == "analyze":
            cmd_analyze(args)
        elif args.command == "baseline":
            cmd_baseline(args)
        else:
            parser.print_help()
            sys.exit(1)
    except RuntimeError as e:
        _error(str(e))
    except Exception as e:
        _error(f"意外错误: {e}")


if __name__ == "__main__":
    main()
