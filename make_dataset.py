#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
带标签数据集生成器（纯标准库）—— 为后续建模准备"有标注"的样本
================================================================
为什么需要它：
    真实轴承数据集（凯斯西储 CWRU）需要从外网下载，且只有几种固定工况。
    本脚本用同一套退化物理模型离线生成**带标签**的时序数据，
    可以自由控制：健康 → 早期缺陷 → 严重劣化 的演化速度、噪声水平、采样率。
    好处：① 断网也能做实验；② 标签一定正确（真实数据标签常需人工对齐）；
         ③ 能造出真实数据集里稀少的"早期微弱故障"样本。

输出的 CSV 每行是一条采样（含 4 个测点 + 标签）：
    ts, temperature_c, vibration_mm_s, speed_rpm, current_a, health(0/1), severity(0.0~1.0), stage

用法：
    python make_dataset.py                              # 默认 3000 条
    python make_dataset.py --n 6000 --out data/dataset.csv
    python make_dataset.py --n 1000 --fault-ratio 0.35
"""

import argparse
import csv
import math
import os
import random
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def generate(n: int, fault_ratio: float, noise: float, seed: int = 42,
             fault_gain: float = 5.0):
    """按健康段/劣化段生成样本，返回 list[dict]

    fault_gain 控制"故障特征有多明显"：
        5.0（默认）= 振动最多抬升 5 mm/s，类间可分性好，阈值基线就能做得很高
        1.2        = 弱故障，与健康分布大量重叠 —— 用来验证"模型是否真的比阈值强"
    """
    rnd = random.Random(seed)
    n_fault = int(n * fault_ratio)
    n_healthy = n - n_fault

    rows = []
    # ---------------- 健康段 ----------------
    t = 0.0
    for _ in range(n_healthy):
        t += 1.0
        temp = 28.5 + 1.2 * math.sin(t / 37.0) + rnd.gauss(0, 0.15 * noise)
        vib = 1.5 + 0.25 * abs(math.sin(t / 11.0)) + abs(rnd.gauss(0, 0.05 * noise))
        rpm = 1480 + 12 * math.sin(t / 23.0) + rnd.gauss(0, 1.5 * noise)
        curr = 8.2 + 0.3 * math.sin(t / 19.0) + rnd.gauss(0, 0.03 * noise)
        rows.append(dict(t=t, temp=temp, vib=vib, rpm=rpm, curr=curr,
                         health=0, severity=0.0, stage="healthy"))

    # ---------------- 劣化段（severity 0→1 线性爬升）----------------
    for i in range(n_fault):
        t += 1.0
        sev = (i + 1) / n_fault                      # 0~1
        # 劣化特征：振动与温度上升，且**冲击成分**随严重度增强（峰度会变高）
        impulse = (rnd.gauss(0, 1.0) ** 2) * 2.2 * sev   # 冲击脉冲
        temp = (28.5 + 1.2 * math.sin(t / 37.0) + 12.0 * sev
                + rnd.gauss(0, 0.15 * noise))
        vib = (1.5 + 0.25 * abs(math.sin(t / 11.0)) + fault_gain * sev + impulse
               + abs(rnd.gauss(0, 0.05 * noise)))
        rpm = 1480 + 12 * math.sin(t / 23.0) + rnd.gauss(0, 1.5 * noise)
        curr = (8.2 + 0.3 * math.sin(t / 19.0) + 1.6 * sev
                + rnd.gauss(0, 0.03 * noise))
        stage = "incipient" if sev < 0.4 else ("moderate" if sev < 0.75 else "severe")
        rows.append(dict(t=t, temp=temp, vib=vib, rpm=rpm, curr=curr,
                         health=1, severity=sev, stage=stage))

    rnd.shuffle(rows)   # 打散，避免模型靠"顺序"作弊
    return rows


def main():
    ap = argparse.ArgumentParser(description="生成带标签的设备退化数据集")
    ap.add_argument("--n", type=int, default=3000, help="总样本数")
    ap.add_argument("--fault-ratio", type=float, default=0.30, help="故障样本占比")
    ap.add_argument("--noise", type=float, default=1.0, help="噪声倍率（1.0 为基准）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--fault-gain", type=float, default=5.0,
                    help="故障特征强度：5.0=明显可分；1.2=弱故障（与健康分布重叠，"
                         "用于检验模型是否真的比阈值基线强）")
    ap.add_argument("--runs", type=int, default=20,
                    help="独立运行段数：每段各自经历 健康→劣化。"
                         "★ E3 修复（Codex task-004）：切分必须按【运行段】而不是按行随机，"
                         "否则同一段退化过程会同时出现在训练集和测试集里 ✗")
    ap.add_argument("--out", default="data/dataset.csv")
    args = ap.parse_args()

    # ★ E3 修复：生成 runs 条【独立】运行段，每段一个独立随机种子，段内按时间排序
    # ★ task-005 复审指出：--n 不能整除 --runs、或 runs 太小，会产生分配不均/退化输入 ✗
    if args.runs < 2:
        sys.exit("[ERROR] --runs 至少为 2（1 段无法做运行段分组切分）")
    if args.n % args.runs != 0:
        sys.exit(f"[ERROR] --n={args.n} 不能被 --runs={args.runs} 整除；"
                 f"请改为 {args.n // args.runs * args.runs} 或 {args.n // args.runs * args.runs + args.runs}")
    per_run = args.n // args.runs
    if per_run < 20:
        sys.exit(f"[ERROR] 每段只有 {per_run} 条，少于 20 条时窗口特征会严重退化；请提高 --n 或降低 --runs")
    rows = []
    for rid in range(1, args.runs + 1):
        part = generate(per_run, args.fault_ratio, args.noise,
                        args.seed + rid * 1000, args.fault_gain)
        for r in part:
            r["run"] = rid
        part.sort(key=lambda r: r["t"])          # 段内恢复真实时间顺序
        rows.extend(part)
    # 不再整体 shuffle：时间顺序是窗口特征因果性的前提 ✅

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["idx", "run", "ts", "temperature_c", "vibration_mm_s", "speed_rpm",
                    "current_a", "health", "severity", "stage"])
        idx = 0
        for r in rows:
            w.writerow([idx, r.get("run", 1), f"t+{int(r['t'])}s", f"{r['temp']:.3f}",
                        f"{r['vib']:.4f}", f"{r['rpm']:.2f}", f"{r['curr']:.3f}",
                        r["health"], f"{r['severity']:.4f}", r["stage"]])
            idx += 1

    # ---------------- 控制台摘要 ----------------
    n_bad = sum(r["health"] for r in rows)
    vib_h = [r["vib"] for r in rows if r["health"] == 0]
    vib_f = [r["vib"] for r in rows if r["health"] == 1]
    stages = {}
    for r in rows:
        stages[r["stage"]] = stages.get(r["stage"], 0) + 1

    print("=" * 62)
    print(f"  数据集已生成 : {os.path.abspath(args.out)}")
    print(f"  总样本       : {len(rows)}   健康 {len(rows)-n_bad} / 故障 {n_bad}")
    print(f"  运行段       : {args.runs} 段（每段 {per_run} 条）—— 切分按 run 分组 ✅")
    print(f"  振动(健康)   : {sum(vib_h)/len(vib_h):.3f} ± "
          f"{math.sqrt(sum((x-sum(vib_h)/len(vib_h))**2 for x in vib_h)/(len(vib_h)-1)):.3f} mm/s")
    print(f"  振动(故障)   : {sum(vib_f)/len(vib_f):.3f} ± "
          f"{math.sqrt(sum((x-sum(vib_f)/len(vib_f))**2 for x in vib_f)/(len(vib_f)-1)):.3f} mm/s")
    print("  阶段分布     : " + "  ".join(f"{k}={v}" for k, v in sorted(stages.items())))
    print("=" * 62)
    print("  下一步: python train_baseline.py --data data/dataset.csv")


if __name__ == "__main__":
    main()
