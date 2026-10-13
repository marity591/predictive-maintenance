#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
特征提取 + 3σ 异常检测 + 自动生成 HTML 报告（纯标准库，零依赖）
================================================================
输入：collector.py 写入的 SQLite（sensor_readings 表）
输出：
    1. 控制台文字摘要（含异常点数、最大偏差倍数）
    2. report.html —— 内联 SVG 折线图（温度/振动/电流 + 异常点标记）+ 统计表
       单文件、无外部资源、可直接截图放进简历/README

用法：
    python analyze.py                                  # 默认读 data/sensor.db
    python analyze.py --db data/sensor.db --out docs/report.html
    python analyze.py --window 5 --sigma 3

为什么用 3σ：
    工业界最常用的"零样本"异常检测基线。对振动这类近似正态的稳态量，
    超出 mean±3σ 的概率约 0.3%，出现即为可疑；它的价值是**不需要标注数据**，
    比"拍脑袋定阈值"更可解释，也是上模型之前的必要对照基线。
"""

import argparse
import html
import math
import os
import sqlite3
import sys
from datetime import datetime

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SERIES = [
    ("temperature_c", "温度 (℃)", "#e8590c", 1.0),
    ("vibration_mm_s", "振动 (mm/s)", "#1971c2", 1.0),
    ("current_a", "电流 (A)", "#2f9e44", 1.0),
]


# ------------------------------------------------------------------ 统计工具
def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs):
    """样本标准差（n-1）"""
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def rms(xs):
    return math.sqrt(sum(x * x for x in xs) / len(xs)) if xs else 0.0


def kurtosis(xs):
    """峰度（超值，正态=0）：冲击类故障的经典指标，轴承早期缺陷会明显抬高它"""
    n = len(xs)
    if n < 4:
        return 0.0
    m = mean(xs)
    s = stdev(xs)
    if s == 0:
        return 0.0
    return sum(((x - m) / s) ** 4 for x in xs) / n - 3.0


def crest_factor(xs):
    """峰值因子 = 峰值 / RMS，冲击成分越多越大"""
    r = rms(xs)
    return (max(abs(x) for x in xs) / r) if r else 0.0


def rolling_windows(rows, key, w):
    """返回 [(结束索引, 窗口值列表)]"""
    out = []
    for i in range(len(rows)):
        lo = max(0, i - w + 1)
        out.append((i, [r[key] for r in rows[lo:i + 1]]))
    return out


# ------------------------------------------------------------------ 数据读取
def load(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cols = ", ".join(k for k, _l, _c, _s in SERIES)
    rows = conn.execute(
        f"SELECT id, ts, epoch, {cols} FROM sensor_readings ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ SVG 折线图
def svg_line_chart(rows, key, label, color, anomalies, width=980, height=220, pad=42):
    if not rows:
        return "<p>（无数据）</p>"
    ys = [r[key] for r in rows]
    ymin, ymax = min(ys), max(ys)
    if ymax - ymin < 1e-9:
        ymax = ymin + 1.0
    span = ymax - ymin
    ymin -= span * 0.08
    ymax += span * 0.08

    def X(i):
        return pad + (width - 2 * pad) * (i / max(1, len(rows) - 1))

    def Y(v):
        return height - pad - (height - 2 * pad) * ((v - ymin) / (ymax - ymin))

    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(ys))

    # 3σ 参考线（全局均值 ± 3σ）
    m, s = mean(ys), stdev(ys)
    refs = ""
    for lvl, name in ((m + 3 * s, "+3σ"), (m - 3 * s, "-3σ")):
        if ymin < lvl < ymax:
            refs += (f'<line x1="{pad}" y1="{Y(lvl):.1f}" x2="{width-pad}" y2="{Y(lvl):.1f}" '
                     f'stroke="#adb5bd" stroke-dasharray="5,4" stroke-width="1"/>'
                     f'<text x="{width-pad-4}" y="{Y(lvl)-4:.1f}" font-size="11" fill="#868e96" '
                     f'text-anchor="end">{name}</text>')

    marks = "".join(
        f'<circle cx="{X(i):.1f}" cy="{Y(ys[i]):.1f}" r="4" fill="none" stroke="#e03131" stroke-width="2"/>'
        for i in anomalies.get(key, [])
    )

    # y 轴刻度
    ticks = ""
    for t in range(5):
        v = ymin + (ymax - ymin) * t / 4
        ticks += (f'<text x="{pad-8}" y="{Y(v)+4:.1f}" font-size="11" fill="#868e96" '
                  f'text-anchor="end">{v:.2f}</text>'
                  f'<line x1="{pad}" y1="{Y(v):.1f}" x2="{width-pad}" y2="{Y(v):.1f}" '
                  f'stroke="#f1f3f5" stroke-width="1"/>')

    return f'''<svg viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px">
  <rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>
  {ticks}{refs}
  <polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"/>
  {marks}
  <text x="{pad}" y="20" font-size="13" fill="#212529" font-weight="600">{html.escape(label)}</text>
  <text x="{width-pad}" y="20" font-size="11" fill="#868e96" text-anchor="end">
    异常点 {len(anomalies.get(key, []))} 个</text>
</svg>'''


# ------------------------------------------------------------------ 主流程
def main():
    ap = argparse.ArgumentParser(description="特征提取 + 3σ 异常检测 + HTML 报告")
    ap.add_argument("--db", default="data/sensor.db")
    ap.add_argument("--out", default="docs/report.html")
    ap.add_argument("--window", type=int, default=5, help="滑动窗口长度（用于时域特征）")
    ap.add_argument("--sigma", type=float, default=3.0, help="异常判定倍数")
    ap.add_argument("--baseline-frac", type=float, default=0.3,
                    help="用前百分之多少的样本作为『健康基线』来估计 μ/σ（默认 0.3）")
    ap.add_argument("--trend-window", type=int, default=10,
                    help="趋势越限检测用的滑动均值窗口（默认 10）")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"  [ERROR] 找不到数据库 {args.db}，请先跑 collector.py")
        sys.exit(1)

    rows = load(args.db)
    if not rows:
        print("[ERROR] 数据库里没有数据"); sys.exit(1)

    print("=" * 62)
    print(f"  数据文件 : {os.path.abspath(args.db)}")
    print(f"  样本条数 : {len(rows)}")
    print(f"  时间范围 : {rows[0]['ts']}  →  {rows[-1]['ts']}")
    print(f"  判定规则 : 健康基线（前 {args.baseline_frac:.0%} 样本）μ±{args.sigma}σ "
          f"+ 趋势越限（滑动均值窗口 {args.trend_window}）")
    print("=" * 62)

    # --- 逐序列做 3σ 异常检测 ---
    # 【重要方法论】基线必须只用"健康段"估计。
    # 若拿整段（含劣化期）算 μ/σ，劣化会被算进"正常波动"里从而检不出来 ——
    # 这是设备监测里最典型的坑。这里取前 baseline_frac 的样本作为健康基线。
    baseline_n = max(5, int(len(rows) * args.baseline_frac))
    print(f"  健康基线 : 取前 {baseline_n} 条（{args.baseline_frac:.0%}）估计 μ/σ")
    anomalies, summary = {}, []
    for key, label, _color, _k in SERIES:
        ys = [r[key] for r in rows]
        base = ys[:baseline_n]
        m, s = mean(base), stdev(base)
        hi, lo = m + args.sigma * s, m - args.sigma * s
        idx = [i for i, v in enumerate(ys) if v > hi or v < lo]

        # 趋势越限：滑动均值越过基线上限（比单点越限更早、更稳）
        trend_idx = []
        for i in range(len(ys)):
            w = ys[max(0, i - args.trend_window + 1): i + 1]
            if len(w) >= max(3, args.trend_window // 2) and mean(w) > hi:
                trend_idx.append(i)
        if trend_idx:
            idx = sorted(set(idx) | set(trend_idx))

        anomalies[key] = idx
        summary.append({
            "key": key, "label": label, "n": len(ys),
            "min": min(ys), "max": max(ys), "mean": mean(ys), "std": stdev(ys),
            "b_mean": m, "b_std": s, "hi": hi, "lo": lo,
            "anom": len(idx), "n_trend": len(trend_idx),
            "max_dev": (max(abs(v - m) for v in ys) / s) if s else 0.0,
        })
        print(f"  {label:<14} 基线 μ={m:8.3f} σ={s:6.3f} | 全段 min={min(ys):7.3f} "
              f"max={max(ys):7.3f} | 越限 {len(idx):3d} 个（含趋势 {len(trend_idx):3d}）")

    # --- 滑动窗口时域特征（在振动上算，工业里最常用） ---
    vkey = "vibration_mm_s"
    feats = []
    for i, w in rolling_windows(rows, vkey, args.window):
        if len(w) >= 3:
            feats.append({"i": i, "rms": rms(w), "peak": max(abs(x) for x in w),
                          "kurt": kurtosis(w), "crest": crest_factor(w)})
    feat_rows = ""
    for f in feats[-12:]:
        feat_rows += (f"<tr><td>{rows[f['i']]['ts']}</td><td>{f['rms']:.3f}</td>"
                      f"<td>{f['peak']:.3f}</td><td>{f['kurt']:.3f}</td>"
                      f"<td>{f['crest']:.2f}</td></tr>")

    # --- 生成 HTML ---
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    charts = "".join(svg_line_chart(rows, k, l, c, anomalies) for k, l, c, _ in SERIES)
    stat_rows = "".join(
        f"<tr><td>{html.escape(s['label'])}</td><td>{s['n']}</td>"
        f"<td>{s['min']:.3f}</td><td>{s['max']:.3f}</td><td>{s['mean']:.3f}</td>"
        f"<td>{s['std']:.3f}</td><td>{s['lo']:.3f} ~ {s['hi']:.3f}</td>"
        f"<td class=\"{'bad' if s['anom'] else 'ok'}\">{s['anom']}</td>"
        f"<td>{s['max_dev']:.2f}σ</td></tr>"
        for s in summary)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>设备健康监测报告 · {os.path.basename(args.db)}</title>
<style>
 body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:0;
      background:#f8f9fa;color:#212529}}
 .wrap{{max-width:1040px;margin:0 auto;padding:28px 20px 60px}}
 h1{{font-size:22px;margin:0 0 6px}} h2{{font-size:16px;margin:28px 0 10px;color:#343a40}}
 .meta{{color:#868e96;font-size:13px;margin-bottom:18px}}
 .card{{background:#fff;border:1px solid #e9ecef;border-radius:10px;padding:14px 16px;
       margin-bottom:18px;box-shadow:0 1px 2px rgba(0,0,0,.03)}}
 table{{border-collapse:collapse;width:100%;font-size:13px}}
 th,td{{border-bottom:1px solid #f1f3f5;padding:7px 8px;text-align:right}}
 th:first-child,td:first-child{{text-align:left}}
 th{{background:#f8f9fa;color:#495057;font-weight:600}}
 td.bad{{color:#e03131;font-weight:700}} td.ok{{color:#2f9e44}}
 .kpi{{display:flex;gap:12px;flex-wrap:wrap}}
 .kpi div{{flex:1 1 150px;background:#fff;border:1px solid #e9ecef;border-radius:10px;
          padding:12px 14px}}
 .kpi b{{display:block;font-size:20px;margin-top:2px}}
 .kpi span{{color:#868e96;font-size:12px}}
 code{{background:#f1f3f5;padding:1px 5px;border-radius:4px;font-size:12px}}
</style></head><body><div class="wrap">
<h1>工业设备健康监测报告</h1>
<div class="meta">数据源 <code>{html.escape(os.path.abspath(args.db))}</code> ·
 生成于 {now} · 规则：健康基线（前 {args.baseline_frac:.0%} 样本）μ±{args.sigma}σ + 趋势越限（滑动均值窗口 {args.trend_window}）</div>

<div class="kpi">
  <div><span>样本条数</span><b>{len(rows)}</b></div>
  <div><span>振动最大偏差</span><b>{summary[1]['max_dev']:.2f}σ</b></div>
  <div><span>振动异常点</span><b>{summary[1]['anom']}</b></div>
  <div><span>温度峰值</span><b>{summary[0]['max']:.1f} ℃</b></div>
</div>

<h2>趋势与异常标记</h2>
<div class="card">{charts}</div>

<h2>统计与异常判定</h2>
<div class="card"><table>
<tr><th>测点</th><th>样本</th><th>最小</th><th>最大</th><th>均值</th><th>标准差</th>
    <th>正常区间</th><th>异常点</th><th>最大偏差</th></tr>
{stat_rows}
</table></div>

<h2>时域特征（振动 · 最近 12 个窗口）</h2>
<div class="card"><table>
<tr><th>时间</th><th>RMS</th><th>峰值</th><th>峰度(超值)</th><th>峰值因子</th></tr>
{feat_rows}
</table>
<p style="font-size:12px;color:#868e96;margin:10px 0 0">
RMS 反映能量水平；峰度与峰值因子对<b>冲击类缺陷</b>（轴承点蚀、剥落）敏感，
往往比 RMS 更早报警 —— 这是后续上模型时最有用的三个特征。</p>
</div>

<h2>结论</h2>
<div class="card" style="font-size:14px;line-height:1.7">
 本次以<b>前 {args.baseline_frac:.0%} 的健康样本</b>建立基线（μ±{args.sigma}σ），
 全段共检出 <b>{sum(s['anom'] for s in summary)}</b> 个越限点（其中振动趋势越限
 {summary[1]['n_trend']} 个）。振动峰值 {summary[1]['max']:.2f} mm/s，
 相对健康基线的最大偏差 {summary[1]['max_dev']:.2f}σ。
 判读：<b>用健康段做基线，劣化 onset 会被立刻抓出来</b>；若把整段（含劣化期）拿来算 μ/σ，
 劣化会被算进"正常波动"从而漏检 —— 这是设备监测最典型的坑。
 下一步：用带标签数据集训练分类模型（阈值基线 → 逻辑回归/SVM → LSTM），
 并与本统计基线做指标对比。
</div>
</div></body></html>"""

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html_doc)

    total_anom = sum(s["anom"] for s in summary)
    print("-" * 62)
    print(f"  合计异常点 : {total_anom} 个")
    print(f"  振动最大偏差: {summary[1]['max_dev']:.2f}σ")
    print(f"  报告已生成 : {os.path.abspath(args.out)}")
    print("=" * 62)


if __name__ == "__main__":
    main()
