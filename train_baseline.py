#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
建模与对比：阈值基线 vs 逻辑回归 vs SVM vs 随机森林
================================================================
输入：make_dataset.py 生成的带标签数据集（data/dataset.csv）
输出：
    1. 控制台 + docs/model_comparison.md 的指标对比表
       （准确率 / 精确率 / 召回率 / F1 / ROC-AUC + 混淆矩阵）
    2. docs/feature_importance.md —— 随机森林特征重要性（可解释性）
    3. models/model.json —— 最优模型的关键超参与指标（便于复现）

运行（推荐用 uv 临时环境，自动走国内镜像）：
    uv run --python 3.12 --with scikit-learn --with numpy python train_baseline.py
或（已装好依赖时）：
    python train_baseline.py --data data/dataset.csv

★ 工程要点（面试可讲）
    1. **特征必须反映"相对健康基线的偏移"**：绝对振动值在不同设备/工况间不可比，
       真实系统会在调试期标定一次基线，之后看"偏离基线多少"。
       这里用训练集健康样本的 μ/σ 生成两个 delta 特征。
    2. **阈值基线是必须存在的对照**：不上模型就报指标是没有意义的；
       而且它常常不差 —— 能说清"模型比阈值好多少、好在哪一类样本上"才是本事。
    3. **分层划分 + 固定随机种子**：保证对比公平、结果可复现。
    4. **只报准确率是耍流氓**：故障检测更关心**召回率**（漏报代价远高于误报）。
    5. **切分必须早于特征拟合**（B3 修复）：健康基线 μ/σ 只能看训练折的健康样本；
       原实现先在全量上算基线，导致测试集信息泄漏、指标偏乐观 ✅
       现在基线是一个 fold-safe 的 sklearn transformer，5 折 CV 会逐折重拟合 ✅
"""

import argparse
import csv
import json
import math
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import numpy as np
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import (accuracy_score, confusion_matrix, f1_score,
                                 precision_score, recall_score, roc_auc_score)
    from sklearn.base import BaseEstimator, TransformerMixin
    from sklearn.model_selection import train_test_split
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.svm import SVC
except ImportError:
    print("[ERROR] 缺少依赖。请用：")
    print("  uv run --python 3.12 --with scikit-learn --with numpy python train_baseline.py")
    sys.exit(1)


# ------------------------------------------------------------------ 工具
def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs):
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def kurtosis(xs):
    n = len(xs)
    if n < 4:
        return 0.0
    m, s = mean(xs), stdev(xs)
    if s == 0:
        return 0.0
    return sum(((x - m) / s) ** 4 for x in xs) / n - 3.0


def load_csv(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({
                "temperature_c": float(r["temperature_c"]),
                "vibration_mm_s": float(r["vibration_mm_s"]),
                "speed_rpm": float(r["speed_rpm"]),
                "current_a": float(r["current_a"]),
                "health": int(r["health"]),
                "severity": float(r["severity"]),
                "run": int(r.get("run", 1)),        # ★ E3：运行段标识
                "_idx": int(r.get("idx", len(rows))),
            })
    # ★ E3 修复：按 (运行段, 时间序) 排好，窗口才有因果意义
    rows.sort(key=lambda r: (r["run"], r["_idx"]))
    return rows


# ------------------------------------------------------------------ 特征工程
def build_base_features(rows, window=10):
    """构造【不含基线偏移】的 8 维基础特征。

    ★ B3 修复（Codex task-002 指出）：原实现在切分之前就用【全量健康样本】
      估计 μ/σ 并生成 vib_delta_base / temp_delta_base，测试集信息泄漏进了特征。
      现在这里只做"不依赖标签"的事（4 个测点 + 4 个因果窗口特征）；
      "相对健康基线的偏移"交给下面的 HealthBaseline —— 它只在【传入的那一份数据】
      的健康样本上拟合，配合 sklearn Pipeline 就能保证 5 折 CV 逐折拟合 ✅
    窗口是**因果**的（只看当前及之前 window-1 个点），不含未来信息 ✅
    """
    # ★ E3 修复（Codex task-004）：窗口【不得跨运行段】——
    #   不同运行段之间没有时间连续性，跨段取窗口等于把两段随意拼接 ✗
    #   按 run 分组、组内按时间顺序各自构造；dict 保持插入顺序，
    #   因此输出顺序与输入顺序一致 → 调用方的 groups/target 数组仍然对齐 ✅
    groups = {}
    for r in rows:
        groups.setdefault(r.get("run", 1), []).append(r)
    X = []
    for g in groups.values():
        vib = [r["vibration_mm_s"] for r in g]
        for i, r in enumerate(g):
            # ★ E2 修复（Codex task-004）：只用"当前及之前已观测到的点"，不偷看未来 ✅
            w = vib[max(0, i - window + 1): i + 1]
            rms = math.sqrt(mean([x * x for x in w]))
            peak = max(abs(x) for x in w)
            X.append([
                r["temperature_c"], r["vibration_mm_s"], r["speed_rpm"], r["current_a"],
                rms, peak, kurtosis(w), (peak / rms) if rms else 0.0,
            ])
    y = [r["health"] for r in rows]
    assert len(X) == len(y), "分组构造后样本数不一致（行顺序契约被破坏）"
    return np.array(X, dtype=float), np.array(y, dtype=int)


BASE_NAMES = ["temperature_c", "vibration_mm_s", "speed_rpm", "current_a",
              "vib_rms_win", "vib_peak_win", "vib_kurt_win", "vib_crest_win"]
DELTA_NAMES = ["vib_delta_base", "temp_delta_base"]


class HealthBaseline(BaseEstimator, TransformerMixin):
    """在【训练折的健康样本】上拟合 μ/σ，再给特征追加两个"偏离基线"维度。

    为什么必须是 transformer 而不是普通函数：
      · fit  只看见训练折（且只用其中 health==0 的行）→ 测试/验证折信息不参与拟合
      · transform 用冻结的 μ/σ → 放进 Pipeline 后，cross_val_predict 每一折都会重新拟合 ✅
    """

    VIB_COL, TEMP_COL = 1, 0

    def fit(self, X, y=None):
        X = np.asarray(X, dtype=float)
        if y is None:
            raise ValueError("HealthBaseline 需要 y（用它筛出健康样本）")
        y = np.asarray(y)
        healthy = X[y == 0]
        if len(healthy) == 0:
            raise ValueError("训练折里没有健康样本，无法建立基线")
        self.mu_vib_ = float(np.mean(healthy[:, self.VIB_COL]))
        self.sigma_vib_ = float(np.std(healthy[:, self.VIB_COL], ddof=1)) or 1.0
        self.mu_temp_ = float(np.mean(healthy[:, self.TEMP_COL]))
        self.n_healthy_ = int(len(healthy))
        return self

    def transform(self, X):
        X = np.asarray(X, dtype=float)
        d_vib = (X[:, self.VIB_COL] - self.mu_vib_) / self.sigma_vib_
        d_tmp = X[:, self.TEMP_COL] - self.mu_temp_
        return np.hstack([X, d_vib.reshape(-1, 1), d_tmp.reshape(-1, 1)])

    # 便于打印/落盘
    def meta(self):
        return {"base_vib_mu": self.mu_vib_, "base_vib_sigma": self.sigma_vib_,
                "base_temp_mu": self.mu_temp_, "n_healthy_fit": self.n_healthy_,
                "threshold_vib": self.mu_vib_ + 3 * self.sigma_vib_}


ALL_NAMES = BASE_NAMES + DELTA_NAMES


# ------------------------------------------------------------------ 评估
def evaluate(name, y_true, y_pred, y_score=None):
    tn, fp, fn, tp = confusion_matrix(y_true, y_pred, labels=[0, 1]).ravel()
    m = {
        "model": name,
        "accuracy": accuracy_score(y_true, y_pred),
        "precision": precision_score(y_true, y_pred, zero_division=0),
        "recall": recall_score(y_true, y_pred, zero_division=0),
        "f1": f1_score(y_true, y_pred, zero_division=0),
        "roc_auc": (roc_auc_score(y_true, y_score) if y_score is not None
                    and len(set(y_true)) > 1 else float("nan")),
        "tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp),
    }
    return m


def fmt_table(results):
    head = ("| 模型 | 准确率 | 精确率 | 召回率 | F1 | ROC-AUC | 混淆矩阵 (TN/FP/FN/TP) |\n"
            "|---|---|---|---|---|---|---|\n")
    lines = ""
    for r in results:
        auc = "—" if math.isnan(r["roc_auc"]) else f"{r['roc_auc']:.4f}"
        lines += (f"| {r['model']} | {r['accuracy']:.4f} | {r['precision']:.4f} | "
                  f"{r['recall']:.4f} | {r['f1']:.4f} | {auc} | "
                  f"{r['tn']}/{r['fp']}/{r['fn']}/{r['tp']} |\n")
    return head + lines


# ------------------------------------------------------------------ 主流程
def main():
    ap = argparse.ArgumentParser(description="阈值基线 vs 机器学习模型对比")
    ap.add_argument("--data", default="data/dataset.csv")
    ap.add_argument("--out", default="docs/model_comparison.md")
    ap.add_argument("--window", type=int, default=10)
    ap.add_argument("--test-size", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    if not os.path.exists(args.data):
        print(f"[ERROR] 找不到数据集 {args.data}，请先跑 make_dataset.py")
        sys.exit(1)

    rows = load_csv(args.data)

    # ★ B3 修复：先切分（按行号分层），再在各自子集上构造窗口特征；
    #   健康基线只在【训练折的健康样本】上拟合，然后冻结、transform 测试折。
    all_y = np.array([r["health"] for r in rows], dtype=int)
    # ★ E3 修复（Codex task-004）：按【运行段】分组切分 ——
    #   原先按行分层随机切分，同一段退化过程会同时出现在训练/测试集里，
    #   且训练与测试在时间上交错（复审 E 组指出）✗
    #   现在：测试集里的运行段在训练时【完全没见过】，这才是能否泛化到新一次运行的诚实评价 ✅
    _runs = np.array([r.get("run", 1) for r in rows])
    from sklearn.model_selection import GroupShuffleSplit
    _gss = GroupShuffleSplit(n_splits=1, test_size=args.test_size, random_state=args.seed)
    idx_tr, idx_te = next(_gss.split(np.zeros(len(rows)), groups=_runs))
    idx_tr, idx_te = np.sort(idx_tr), np.sort(idx_te)
    print(f"  切分方式 : 按运行段分组（训练 {len(set(_runs[idx_tr]))} 段 / "
          f"测试 {len(set(_runs[idx_te]))} 段，测试段训练时未见过 ✅）")
    tr_rows = [rows[i] for i in idx_tr]
    te_rows = [rows[i] for i in idx_te]
    Xtr, ytr = build_base_features(tr_rows, args.window)
    Xte, yte = build_base_features(te_rows, args.window)
    names = ALL_NAMES
    hb = HealthBaseline().fit(Xtr, ytr)                        # 只喂训练折
    meta = hb.meta()                                           # 仅取基线数值用于报告
    # ★ E1 修复（Codex task-004）：这里【不能】再 transform ——
    #   三个模型都是 Pipeline(HealthBaseline(), ...)，管线内部会追加 delta 两列；
    #   原来在切分后又 transform 一次，模型实际用的是 12 维（delta 重复两遍），
    #   而 feature_importance 只按 10 个名字输出 → 特征描述与重要性报告不一致 ✗
    y = all_y

    print("=" * 74)
    print(f"  数据集   : {os.path.abspath(args.data)}  样本 {len(rows)}  "
          f"(健康 {int((y==0).sum())} / 故障 {int((y==1).sum())})")
    print(f"  特征维度 : 基础 {Xtr.shape[1]} 维 + 管线追加 {len(DELTA_NAMES)} 维 = {len(names)} 维"
          f"  →  {', '.join(names)}")
    print(f"  健康基线 : 振动 μ={meta['base_vib_mu']:.3f} σ={meta['base_vib_sigma']:.3f} "
          f"→ 阈值 μ+3σ = {meta['threshold_vib']:.3f} mm/s")
    print("=" * 74)


    results = []

    # ---------- ① 阈值基线（把"振动高于健康基线 μ+3σ"当作故障）----------
    thr = meta["threshold_vib"]
    y_pred_thr = (Xte[:, names.index("vibration_mm_s")] > thr).astype(int)
    results.append(evaluate("① 阈值基线 (振动 > μ+3σ)", yte, y_pred_thr, None))

    # ---------- ② 逻辑回归 ----------
    lr = make_pipeline(HealthBaseline(), StandardScaler(),
                       LogisticRegression(max_iter=2000, random_state=args.seed))
    lr.fit(Xtr, ytr)
    results.append(evaluate("② 逻辑回归", yte, lr.predict(Xte), lr.predict_proba(Xte)[:, 1]))

    # ---------- ③ SVM (RBF) ----------
    # 注意：sklearn 1.9 起 SVC(probability=True) 已弃用（1.11 移除），
    # 改用 decision_function 的置信度算 ROC-AUC，效果等价且无告警。
    svm = make_pipeline(HealthBaseline(), StandardScaler(),
                        SVC(kernel="rbf", C=10, gamma="scale", random_state=args.seed))
    svm.fit(Xtr, ytr)
    results.append(evaluate("③ SVM (RBF)", yte, svm.predict(Xte), svm.decision_function(Xte)))

    # ---------- ④ 随机森林 ----------
    rf = make_pipeline(HealthBaseline(),
                       RandomForestClassifier(n_estimators=300, random_state=args.seed, n_jobs=-1))
    rf.fit(Xtr, ytr)
    results.append(evaluate("④ 随机森林 (300 树)", yte, rf.predict(Xte), rf.predict_proba(Xte)[:, 1]))

    # ---------- 输出对比表 ----------
    table = fmt_table(results)
    print("  测试集指标对比（测试集 %d 条）" % len(yte))
    print()
    for line in table.strip().split("\n"):
        print("  " + line)
    print()

    best = max(results, key=lambda r: (r["f1"], r["recall"]))
    base = results[0]
    print(f"  最优模型 : {best['model']}   F1={best['f1']:.4f}  召回={best['recall']:.4f}")
    print(f"  相对阈值基线提升: F1 {(best['f1']-base['f1'])*100:+.2f} 个百分点，"
          f"召回 {(best['recall']-base['recall'])*100:+.2f} 个百分点")
    print(f"  漏报(把故障判成健康) : 基线 {base['fn']} 个 → 最优 {best['fn']} 个")
    print("=" * 74)

    # ---------- 落盘 ----------
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("# 模型对比结果\n\n")
        f.write(f"- 数据集：`{os.path.basename(args.data)}`（{len(rows)} 条，"
                f"健康 {int((y==0).sum())} / 故障 {int((y==1).sum())}）\n")
        f.write(f"- 训练/测试：{len(ytr)} / {len(yte)}（分层抽样，seed={args.seed}）\n")
        f.write(f"- 特征（{len(names)} 维）：{', '.join('`'+n+'`' for n in names)}\n")
        f.write(f"- 健康基线：振动 μ={meta['base_vib_mu']:.3f}、σ={meta['base_vib_sigma']:.3f}，"
                f"阈值 μ+3σ={meta['threshold_vib']:.3f} mm/s\n\n")
        f.write("## 指标对比\n\n" + table + "\n")
        f.write("> 故障检测场景下**召回率比准确率更重要**：漏报（FN）意味着设备带着故障继续跑。\n\n")
        f.write(f"**最优模型**：{best['model']}（F1={best['f1']:.4f}，召回={best['recall']:.4f}）\n")

    os.makedirs("models", exist_ok=True)
    # ★ B6 修复（Codex task-002）：models/ 在 .gitignore 里，
    # 全新 clone 没有该目录 → 原来 open() 会直接 FileNotFoundError ✗
    os.makedirs("models", exist_ok=True)
    with open("models/model.json", "w", encoding="utf-8") as f:
        json.dump({"features": names, "baseline": meta,
                   "best_model": best["model"], "metrics": best,
                   "all_results": results}, f, ensure_ascii=False, indent=2)

    # ---------- 特征重要性 ----------
    imp = sorted(zip(names, rf[-1].feature_importances_), key=lambda t: -t[1])
    with open("docs/feature_importance.md", "w", encoding="utf-8") as f:
        f.write("# 随机森林特征重要性\n\n| 特征 | 重要性 |\n|---|---|\n")
        for n, v in imp:
            f.write(f"| `{n}` | {v:.4f} |\n")
    print("  已写出 : " + os.path.abspath(args.out))
    print("  已写出 : " + os.path.abspath("docs/feature_importance.md"))
    print("  已写出 : " + os.path.abspath("models/model.json"))
    print()
    print("  特征重要性 Top5 :")
    for n, v in imp[:5]:
        print(f"    {n:<18} {v:.4f}")


if __name__ == "__main__":
    main()
