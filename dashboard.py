#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
看板生成器（零依赖单文件 HTML Dashboard）—— 云侧长时段视图
================================================================
与 analyze.py 的分工（两者刻意分开，体现"边缘 vs 云"的不同职责）：
    analyze.py   : 边缘侧，针对**单次采集**做特征+3σ+报告，关注"这次有没有异常"
    dashboard.py : 云侧，查**时序库聚合表**看长时段趋势（24h/7d），关注"整体是否在劣化"

为什么不用 Grafana：
    Grafana 需要额外跑一个服务、配数据源、写面板 JSON。本项目优先保证
    "clone 下来就能跑、离线可看、能截图进简历"，所以先生成一个**自包含 HTML**：
      · 纯标准库，内联 SVG，无 CDN、无 JS 依赖 → 断网也能打开、可直接提交进仓库
      · 需要的话再加 --refresh 秒数，页面自动刷新，当"值班大屏"用
    真上生产时把本文件换成 Grafana + InfluxDB/TDengine 数据源即可，查询语义一致。

面板设计：
    · KPI 行：测点数 / 时间跨度 / 桶数 / 异常桶数 / 压缩比 / 磁盘占用
    · 每个测点一个面板：均值折线 + min~max 包络带 + 健康基线 μ±3σ + 异常标记
    · 最近 N 个桶的明细表（含推导出的标准差）

用法：
    python dashboard.py --db data/tsdb.db --device line1 --hours 24 --out docs/dashboard.html
    python dashboard.py --hours 168 --refresh 30        # 7 天窗口 + 30 秒自动刷新
"""

import argparse
import datetime as dt
import math
import os
import sqlite3
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# (测点名, 显示名, 颜色, 显示系数)
# * 显示系数必须为 1.0：库里的值在采集阶段就已经换算成工程量了
#   （collector.py 用 0.1/0.01 把寄存器原始整数换算成 ℃/mm/s/A）。
#   这里曾经误填 100/10，导致看板上振动显示成 386 mm/s —— 典型的
#   "标定系数用了两次"错误，两个阶段各换算一次就放大成 10000 倍。
PANELS = [
    ("vibration_mm_s", "振动 (mm/s)", "#1971c2", 1.0),
    ("temperature_c", "温度 (℃)", "#e8590c", 1.0),
    ("current_a", "电流 (A)", "#2f9e44", 1.0),
    ("speed_rpm", "转速 (rpm)", "#7048e8", 1.0),
]


# ---------------------------------------------------------------- 数据
def load_buckets(conn, device, metric, start, end, interval_s=60):
    rows = conn.execute(
        "SELECT bucket_epoch, avg, min, max, cnt, sum_sq FROM ts_agg "
        "WHERE device=? AND metric=? AND interval_s=? AND bucket_epoch>=? AND bucket_epoch<? "
        "ORDER BY bucket_epoch",
        (device, metric, interval_s, int(start // interval_s) * interval_s,
         int(end // interval_s) * interval_s + interval_s)).fetchall()
    return [dict(r) for r in rows]


def load_db_stats(conn, db_path):
    raw = conn.execute("SELECT COUNT(*), MIN(epoch), MAX(epoch) FROM ts_raw").fetchone()
    agg = conn.execute("SELECT COUNT(*) FROM ts_agg").fetchone()
    size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    return {"raw": raw[0], "t0": raw[1], "t1": raw[2], "agg": agg[0], "bytes": size}


# ---------------------------------------------------------------- 统计
def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs):
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def std_from(sum_sq, avg, cnt):
    if not cnt or cnt < 2 or sum_sq is None or avg is None:
        return 0.0
    return math.sqrt(max(0.0, sum_sq / cnt - avg * avg))


def fmt_ts(ep, pattern="%m-%d %H:%M"):
    try:
        return "" if ep is None else time.strftime(pattern, time.localtime(ep))
    except (OSError, OverflowError, ValueError, TypeError):
        return "—"      # Windows 的 localtime 不接受负时间戳；不再让整份看板崩掉


# ---------------------------------------------------------------- SVG 面板
def valid_rows(rows):
    """只保留 avg/min/max 都非 NULL 的桶。

    * P5 修复：上一轮我只在 panel_svg 里过滤缺测桶，
      但主流程统计与明细表仍直接用全量 rows —— 遇到"整桶缺测"时
      mean([None,...]) 会抛 TypeError、明细表格式化 None 也会崩。
      现在所有消费方共用这一个函数 [ok]
    """
    return [r for r in rows
            if r["avg"] is not None and r["min"] is not None and r["max"] is not None]


def panel_svg(rows, label, color, scale, sigma=3.0, width=1040, height=250, pad=54):
    if not rows:
        return f"<p style='color:#868e96'>（{label} 无数据）</p>"

    rows = valid_rows(rows)   # 缺测桶不能进图（见 valid_rows 注释）
    if not rows:
        return f"<p style='color:#868e96'>（{label} 无有效数据：窗口内该测点全部缺测）</p>"

    xs = [r["bucket_epoch"] for r in rows]
    avgs = [r["avg"] for r in rows]
    mins = [r["min"] for r in rows]
    maxs = [r["max"] for r in rows]

    # 健康基线：取窗口前 30% 的桶（与 analyze.py 的原则一致：基线只能用健康段）
    # [warn] 这【不保证是健康段】：若窗口起点已在劣化期，
    #    基线会被劣化值污染、越限直接归零（实测 10 个越限 → 0 个）。
    #    需要可靠基线时请指定已知健康时段或改用持久化标定参数；
    #    没有健康依据时应把它当作窗口参考，而不是正常水平。
    base_n = max(3, int(len(rows) * 0.3))
    bm, bs = mean(avgs[:base_n]), stdev(avgs[:base_n])
    hi, lo = bm + sigma * bs, bm - sigma * bs
    anomalies = [i for i, v in enumerate(avgs) if v > hi or v < lo]

    ymin = min(min(mins), lo)
    ymax = max(max(maxs), hi)
    span = ymax - ymin
    if span <= 1e-12:      # 常量序列也要有可画的纵轴范围
        span = max(abs(bm) * 1e-6, 1e-9)
    ymin -= span * 0.06
    ymax += span * 0.06

    # 横坐标按【真实时间】定位：原来按序号均分，
    # 缺采的间隔会被画成等距，斜率与断采都被歪曲（实测 60s 与 3540s 两段等宽）
    t_span = (xs[-1] - xs[0]) or 1.0

    def X(i):
        return pad + (width - 2 * pad) * ((xs[i] - xs[0]) / t_span)

    def Y(v):
        return height - pad - (height - 2 * pad) * ((v - ymin) / (ymax - ymin))

    avg_pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(avgs))
    band = (" ".join(f"{X(i):.1f},{Y(maxs[i]):.1f}" for i in range(len(rows)))
            + " " + " ".join(f"{X(i):.1f},{Y(mins[i]):.1f}" for i in range(len(rows) - 1, -1, -1)))

    grid = ""
    for t in range(5):
        v = ymin + (ymax - ymin) * t / 4
        grid += (f'<line x1="{pad}" y1="{Y(v):.1f}" x2="{width-pad}" y2="{Y(v):.1f}" stroke="#f1f3f5"/>'
                 f'<text x="{pad-8}" y="{Y(v)+4:.1f}" font-size="11" fill="#868e96" text-anchor="end">'
                 f'{v*scale:.2f}</text>')

    refs = ""
    for lvl, nm in ((hi, "+3σ"), (lo, "-3σ")):
        if ymin < lvl < ymax:
            refs += (f'<line x1="{pad}" y1="{Y(lvl):.1f}" x2="{width-pad}" y2="{Y(lvl):.1f}" '
                     f'stroke="#e03131" stroke-dasharray="6,4" stroke-width="1" opacity="0.6"/>'
                     f'<text x="{width-pad-4}" y="{Y(lvl)-4:.1f}" font-size="11" fill="#e03131" '
                     f'text-anchor="end">{nm}</text>')

    marks = "".join(
        f'<circle cx="{X(i):.1f}" cy="{Y(avgs[i]):.1f}" r="4" fill="none" stroke="#e03131" stroke-width="2"/>'
        for i in anomalies)
    base_line = (f'<line x1="{pad}" y1="{Y(bm):.1f}" x2="{width-pad}" y2="{Y(bm):.1f}" '
                 f'stroke="{color}" stroke-dasharray="3,3" opacity="0.5"/>')

    # 时间轴：首/中/尾
    tlab = ""
    for idx, anchor in ((0, "start"), (len(rows) // 2, "middle"), (len(rows) - 1, "end")):
        tlab += (f'<text x="{X(idx):.1f}" y="{height-pad+20:.1f}" font-size="11" fill="#868e96" '
                 f'text-anchor="{anchor}">{fmt_ts(xs[idx])}</text>')

    return f'''<svg viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px">
<rect width="{width}" height="{height}" fill="#fff"/>
{grid}{refs}
<polygon points="{band}" fill="{color}" opacity="0.13"/>
<line x1="{pad}" y1="{Y(bm):.1f}" x2="{width-pad}" y2="{Y(bm):.1f}" stroke="{color}" stroke-dasharray="3,3" opacity="0.5"/>
<polyline points="{avg_pts}" fill="none" stroke="{color}" stroke-width="2"/>
{marks}{tlab}
<text x="{pad}" y="22" font-size="13.5" font-weight="600" fill="#212529">{label}</text>
<text x="{width-pad}" y="22" font-size="11.5" fill="#868e96" text-anchor="end">
  基线 μ={bm*scale:.3f}（窗口前30%，仅供参考）· 桶 {len(rows)} 个 · 越限 {len(anomalies)} 个 · 峰值 {max(maxs)*scale:.2f}</text>
</svg>'''


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="零依赖单文件看板生成器")
    ap.add_argument("--db", default="data/tsdb.db")
    ap.add_argument("--device", default="line1")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--out", default="docs/dashboard.html")
    ap.add_argument("--refresh", type=int, default=0, help="自动刷新秒数（0=不刷新）")
    ap.add_argument("--detail", type=int, default=8, help="明细表显示最近多少个桶")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"[ERROR] 找不到 {args.db}（先跑 tsdb.py seed-demo / rollup，或 run_demo.ps1）")
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    t0 = time.perf_counter()
    st = load_db_stats(conn, args.db)
    end = st["t1"] or time.time()
    start = max(0.0, end - args.hours * 3600)   # * P5：防止负时间戳让 localtime 报错

    data, total_anom = {}, 0
    print("=" * 74)
    print(f"  看板生成  时序库 {os.path.abspath(args.db)}  设备 {args.device}")
    print(f"  窗口      {fmt_ts(start, '%Y-%m-%d %H:%M')} → {fmt_ts(end, '%Y-%m-%d %H:%M')}"
          f"（{args.hours:.0f} 小时，桶宽 {args.interval}s）")
    print("-" * 74)
    for metric, label, _c, scale in PANELS:
        rows = load_buckets(conn, args.device, metric, start, end, args.interval)
        data[metric] = rows
        ok = valid_rows(rows)          # * P5：统计只用有效桶
        if ok:
            base_n = max(3, int(len(ok) * 0.3))
            bm, bs = mean([r["avg"] for r in ok[:base_n]]), stdev([r["avg"] for r in ok[:base_n]])
            anom = sum(1 for r in ok if r["avg"] > bm + 3 * bs or r["avg"] < bm - 3 * bs)
            total_anom += anom
            skip = "" if len(ok) == len(rows) else f"（另有 {len(rows)-len(ok)} 个缺测桶未计入统计）"
            print(f"  {label:<14} 桶 {len(ok):>5}  均值 {mean([r['avg'] for r in ok])*scale:>9.3f}"
                  f"  峰 {max(r['max'] for r in ok)*scale:>9.3f}  越限 {anom:>4}{skip}")
        elif rows:
            print(f"  {label:<14} 桶 {len(rows):>5} 但全部缺测（avg/min/max 为 NULL）→ 不参与统计")
        else:
            print(f"  {label:<14} 无数据")

    # ---------------- KPI ----------------
    ratio = (st["raw"] / st["agg"]) if st["agg"] else 0
    kpi = [
        ("测点数", str(len(PANELS))),
        ("原始点", f"{st['raw']:,}"),
        ("聚合桶", f"{st['agg']:,}"),
        ("表行数比", f"1/{ratio:.1f}" if ratio else "—"),
        ("趋势点数减少", f"{ratio*len(PANELS):.0f}×" if ratio else "—"),
        ("窗口内越限桶", str(total_anom)),
        ("库文件", f"{st['bytes']/1024:.0f} KB"),
    ]

    # ---------------- 明细表 ----------------
    detail_rows = ""
    vib = valid_rows(data.get("vibration_mm_s", []))   # * P5
    for r in vib[-args.detail:][::-1]:
        sd = std_from(r["sum_sq"], r["avg"], r["cnt"])
        # 阈值 0.5 的量纲是 mm/s（本表只列振动）；它是经验值，应按设备标定
        flag = "[warn]" if sd > 0.5 else ""
        detail_rows += (f"<tr><td>{fmt_ts(r['bucket_epoch'], '%m-%d %H:%M')}</td>"
                        f"<td>{r['avg']:.4f}</td><td>{r['min']:.4f}</td><td>{r['max']:.4f}</td>"
                        f"<td>{sd:.4f}</td><td>{r['cnt']}</td><td>{flag}</td></tr>")

    panels = "".join(panel_svg(data.get(m, []), lab, c, sc)
                     for m, lab, c, sc in PANELS)
    kpi_html = "".join(f"<div><span>{k}</span><b>{v}</b></div>" for k, v in kpi)
    refresh_meta = (f'<meta http-equiv="refresh" content="{args.refresh}">' if args.refresh else "")
    gen_ms = (time.perf_counter() - t0) * 1000

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">{refresh_meta}
<title>设备健康看板 · {args.device}</title>
<style>
 body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:0;background:#f8f9fa;color:#212529}}
 .wrap{{max-width:1120px;margin:0 auto;padding:26px 20px 70px}}
 h1{{font-size:21px;margin:0 0 4px}} h2{{font-size:15px;margin:26px 0 10px;color:#343a40}}
 .meta{{color:#868e96;font-size:12.5px;margin-bottom:16px}}
 .kpi{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:6px}}
 .kpi div{{flex:1 1 150px;background:#fff;border:1px solid #e9ecef;border-radius:10px;padding:11px 14px}}
 .kpi b{{display:block;font-size:19px;margin-top:3px}}
 .kpi span{{color:#868e96;font-size:11.5px}}
 .card{{background:#fff;border:1px solid #e9ecef;border-radius:10px;padding:12px;margin-bottom:16px}}
 table{{border-collapse:collapse;width:100%;font-size:12.5px}}
 th,td{{border-bottom:1px solid #f1f3f5;padding:6px 8px;text-align:right}}
 th:first-child,td:first-child{{text-align:left}} th{{background:#f8f9fa;color:#495057}}
 .note{{font-size:12px;color:#868e96;line-height:1.7}}
 code{{background:#f1f3f5;padding:1px 5px;border-radius:4px;font-size:12px}}
</style></head><body><div class="wrap">
<h1>工业设备健康看板</h1>
<div class="meta">设备 <code>{args.device}</code> · 时序库 <code>{os.path.abspath(args.db)}</code> ·
 窗口 {fmt_ts(start,'%Y-%m-%d %H:%M')} → {fmt_ts(end,'%Y-%m-%d %H:%M')} ·
 生成于 {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}（{gen_ms:.0f} ms）
 {'· 每 '+str(args.refresh)+' 秒自动刷新' if args.refresh else ''}</div>

<div class="kpi">{kpi_html}</div>

<h2>趋势面板（均值折线 + min~max 包络 + 健康基线 μ±3σ + 越限标记）</h2>
<div class="card">{panels}</div>

<h2>振动明细（最近 {args.detail} 个桶，标准差由 sum_sq 推导）</h2>
<div class="card"><table>
<tr><th>桶起点</th><th>均值</th><th>最小</th><th>最大</th><th>标准差</th><th>样本数</th><th>提示</th></tr>
{detail_rows}
</table>
<p class="note" style="margin:10px 0 0">
趋势面板查的是<b>聚合表</b>（1 分钟桶），所以 24 小时窗口只需扫 1440×4 行；<br>
需要看某一秒的细节时，再回查 <code>ts_raw</code> 原始表 —— 这就是"原始短保留 + 聚合长保留"的用法。</p>
</div>

<h2>判读说明</h2>
<div class="card note">
· <b>基线只能用健康段</b>：这里取窗口前 30% 的桶估计 μ/σ。若拿整段（含劣化期）估计，
  劣化会被算进"正常波动"从而漏检 —— 这是设备监测最典型的坑。<br>
· <b>包络带</b>（浅色区域）是每个桶的 min~max，用于判断"波动是否变大"：
  均值没变但包络变宽，往往意味着冲击成分增加（轴承早期缺陷的典型表现）。<br>
· <b>标准差列</b>由 <code>sum_sq</code> 推导，标准差明显偏大的桶（&gt;0.5）标了，值得回查原始点。
</div>
</div></body></html>"""

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)

    print("-" * 74)
    print(f"  窗口内越限桶合计 : {total_anom}")
    print(f"  生成耗时         : {gen_ms:.0f} ms")
    print(f"  看板已生成       : {os.path.abspath(args.out)}"
          f"（{os.path.getsize(args.out)/1024:.1f} KB）")
    print("=" * 74)
    conn.close()


if __name__ == "__main__":
    main()
