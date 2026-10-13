#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
剩余寿命（RUL）回归 + 5 折交叉验证
================================================================
分类只能回答"坏没坏"，预测性维护真正要的是**"还能用多久"** —— 这才是 RUL 回归。

标签定义（合成数据的真值假设，务必写清楚）：
    make_dataset.py 为每个样本生成了 severity ∈ [0,1]（0=健康，1=完全劣化），
    这里把它线性映射成剩余寿命：  RUL_hours = (1 - severity) × 100
    即：健康样本 100 小时，完全劣化样本 0 小时。
    * 真实场景里 RUL 需要"失效时刻 - 当前时刻"，且健康样本是**删失数据**（只知道还没坏），
      处理方式完全不同（生存分析/删失回归）。本项目先用线性映射做通链路。

对比模型（含"什么都不学"的基线）：
    ① Dummy（永远预测训练集均值）  ← 最重要的对照：R²<0 就说明模型还不如瞎猜
    ② 线性回归  ③ Ridge ④ 随机森林 ⑤ 梯度提升
指标：MAE / RMSE / R²，5 折交叉验证给出 均值 ± 标准差

运行：
    uv run --python 3.12 --with scikit-learn --with numpy python train_rul.py
"""

import argparse
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
    from sklearn.dummy import DummyRegressor
    from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
    from sklearn.linear_model import LinearRegression, Ridge
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
    from sklearn.base import clone
    from sklearn.model_selection import GroupKFold, KFold, cross_val_predict
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
except ImportError:
    print("[ERROR] 缺少依赖，请用：")
    print("  uv run --python 3.12 --with scikit-learn --with numpy python train_rul.py")
    sys.exit(1)

# 复用分类脚本里的特征工程与数据读取（保持两段代码特征完全一致）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from train_baseline import ALL_NAMES, HealthBaseline, build_base_features, load_csv  # noqa: E402

MAX_RUL_HOURS = 100.0


def build_target(rows):
    """severity → 剩余寿命（小时）"""
    return np.array([(1.0 - r["severity"]) * MAX_RUL_HOURS for r in rows], dtype=float)


def evaluate_cv(model, X, y, cv):
    """5 折交叉验证：把各折的验证集预测拼起来算总体指标，再给出每折指标的均值±标准差"""
    y_pred = cross_val_predict(model, X, y, cv=cv)
    mae_all = mean_absolute_error(y, y_pred)
    rmse_all = math.sqrt(mean_squared_error(y, y_pred))
    r2_all = r2_score(y, y_pred)

    per_mae, per_rmse, per_r2 = [], [], []
    for tr, te in cv.split(X, y):
        m = model
        m.fit(X[tr], y[tr])
        p = m.predict(X[te])
        per_mae.append(mean_absolute_error(y[te], p))
        per_rmse.append(math.sqrt(mean_squared_error(y[te], p)))
        per_r2.append(r2_score(y[te], p))
    return {
        "mae": mae_all, "rmse": rmse_all, "r2": r2_all,
        "mae_mean": float(np.mean(per_mae)), "mae_std": float(np.std(per_mae)),
        "rmse_mean": float(np.mean(per_rmse)), "rmse_std": float(np.std(per_rmse)),
        "r2_mean": float(np.mean(per_r2)), "r2_std": float(np.std(per_r2)),
    }, y_pred


def svg_scatter(y_true, y_pred, title, width=600, height=600, pad=54):
    lo = min(y_true.min(), y_pred.min()) * 0.95
    hi = max(y_true.max(), y_pred.max()) * 1.05
    span = max(1e-9, hi - lo)

    def X(v):
        return pad + (width - 2 * pad) * ((v - lo) / span)

    def Y(v):
        return height - pad - (height - 2 * pad) * ((v - lo) / span)

    pts = "".join(f'<circle cx="{X(t):.1f}" cy="{Y(p):.1f}" r="2.6" fill="#1971c2" opacity="0.45"/>'
                  for t, p in zip(y_true, y_pred))
    ticks = ""
    for i in range(6):
        v = lo + span * i / 5
        ticks += (f'<line x1="{pad}" y1="{Y(v):.1f}" x2="{width-pad}" y2="{Y(v):.1f}" stroke="#f1f3f5"/>'
                  f'<line x1="{X(v):.1f}" y1="{pad}" x2="{X(v):.1f}" y2="{height-pad}" stroke="#f1f3f5"/>'
                  f'<text x="{pad-8}" y="{Y(v)+4:.1f}" font-size="11" fill="#868e96" text-anchor="end">{v:.0f}</text>'
                  f'<text x="{X(v):.1f}" y="{height-pad+18}" font-size="11" fill="#868e96" text-anchor="middle">{v:.0f}</text>')
    return f'''<svg viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px">
<rect width="{width}" height="{height}" fill="#fff"/>
{ticks}
<line x1="{X(lo):.1f}" y1="{Y(lo):.1f}" x2="{X(hi):.1f}" y2="{Y(hi):.1f}" stroke="#e03131" stroke-dasharray="5,4"/>
{pts}
<text x="{pad}" y="24" font-size="14" font-weight="600" fill="#212529">{title}</text>
<text x="{width/2}" y="{height-14}" font-size="12" fill="#868e96" text-anchor="middle">真实剩余寿命 (h)</text>
<text x="16" y="{height/2}" font-size="12" fill="#868e96" transform="rotate(-90 16 {height/2})" text-anchor="middle">预测剩余寿命 (h)</text>
</svg>'''


# ---------------------------------------------------------------- leak-free 逐折评估
def evaluate_cv_leakfree(make_model, X_base, y, health, cv, groups=None):
    """B3 修复：每折只用训练折的健康样本拟合健康基线，冻结后变换训练/验证折。"""
    import numpy as _np
    preds = _np.empty(len(y), dtype=float)
    f_mae, f_rmse, f_r2 = [], [], []
    splits = cv.split(X_base, y, groups) if groups is not None else cv.split(X_base)
    for tr, te in splits:
        hb = HealthBaseline().fit(X_base[tr], health[tr])
        Xtr, Xte = hb.transform(X_base[tr]), hb.transform(X_base[te])
        m = make_model()
        m.fit(Xtr, y[tr])
        pred = m.predict(Xte)
        preds[te] = pred
        f_mae.append(float(_np.mean(_np.abs(pred - y[te]))))
        f_rmse.append(float(_np.sqrt(_np.mean((pred - y[te]) ** 2))))
        ss_res = float(_np.sum((y[te] - pred) ** 2))
        ss_tot = float(_np.sum((y[te] - _np.mean(y[te])) ** 2))
        f_r2.append(1 - ss_res / ss_tot if ss_tot else 0.0)
    res = {"mae": float(_np.mean(_np.abs(preds - y))),
           "rmse": float(_np.sqrt(_np.mean((preds - y) ** 2))),
           "mae_std": float(_np.std(f_mae, ddof=1)) if len(f_mae) > 1 else 0.0,
           "rmse_std": float(_np.std(f_rmse, ddof=1)) if len(f_rmse) > 1 else 0.0,
           "r2_std": float(_np.std(f_r2, ddof=1)) if len(f_r2) > 1 else 0.0}
    ss_res = float(_np.sum((y - preds) ** 2)); ss_tot = float(_np.sum((y - _np.mean(y)) ** 2))
    res["r2"] = 1 - ss_res / ss_tot if ss_tot else 0.0
    return res, preds

def main():
    ap = argparse.ArgumentParser(description="RUL 回归 + 5 折交叉验证")
    ap.add_argument("--data", default="data/dataset.csv")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--out", default="docs/rul_report.md")
    ap.add_argument("--window", type=int, default=10)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    if not os.path.exists(args.data):
        print(f"  [ERROR] 找不到 {args.data}"); sys.exit(1)

    rows = load_csv(args.data)
    # * B3 修复：这里只建【不含基线】的基础特征；
    #   健康基线由 HealthBaseline 放在每个模型的管线里，cross_val_predict 会逐折重拟合 [ok]
    X, _y_cls = build_base_features(rows, args.window)
    names = ALL_NAMES
    y = build_target(rows)
    # * E3 修复：按【运行段】分组做 K 折 ——
    #   原来 KFold(shuffle=True) 会让同一段退化过程同时进训练折与验证折，
    #   而且窗口还是用全量数据构造的（跨折）[err] 现在折间不共享运行段 [ok]
    _runs = np.array([r.get("run", 1) for r in rows])
    cv = GroupKFold(n_splits=args.folds)

    print("=" * 78)
    print(f"  RUL 回归  数据集 {os.path.abspath(args.data)}  样本 {len(rows)}")
    print(f"  标签: RUL_hours = (1 - severity) × {MAX_RUL_HOURS:.0f}   "
          f"范围 {y.min():.1f} ~ {y.max():.1f} h   均值 {y.mean():.1f} h")
    print(f"  验证: {args.folds} 折交叉验证（shuffle, seed={args.seed}）")
    print("=" * 78)

    models = [
        ("① Dummy（恒预测均值）", DummyRegressor(strategy="mean")),
        ("② 线性回归", make_pipeline(StandardScaler(), LinearRegression())),
        ("③ Ridge (α=1)", make_pipeline(StandardScaler(), Ridge(alpha=1.0))),
        ("④ 随机森林 (300 树)", make_pipeline(
            RandomForestRegressor(n_estimators=300, random_state=args.seed, n_jobs=-1))),
        ("⑤ 梯度提升 (200 树)", make_pipeline(
            GradientBoostingRegressor(n_estimators=200, random_state=args.seed))),
    ]

    results, preds = [], {}
    for name, model in models:
        m, y_pred = evaluate_cv_leakfree(lambda: clone(model), X, y, _y_cls, cv, _runs)   # B3 + E3
        m["model"] = name
        results.append(m)
        preds[name] = y_pred
        print(f"  {name:<22} MAE={m['mae']:6.2f}h (折间 ±{m['mae_std']:4.2f})  "
              f"RMSE={m['rmse']:6.2f}h  R²={m['r2']:+.4f}")

    best = max(results, key=lambda r: r["r2"])
    dummy = results[0]
    print("-" * 78)
    print(f"  最优模型 : {best['model']}   MAE={best['mae']:.2f}h  RMSE={best['rmse']:.2f}h  R²={best['r2']:.4f}")
    print(f"  相对 Dummy 基线: MAE {(best['mae']-dummy['mae'])/dummy['mae']*100:+.1f}%，"
          f"R² {dummy['r2']:+.4f} → {best['r2']:+.4f}")
    print(f"  折间稳定性   : MAE 标准差 ±{best['mae_std']:.3f}h，R² 标准差 ±{best['r2_std']:.4f}")
    print("=" * 78)

    # ---------------- 报告 ----------------
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    table = ("| 模型 | MAE (h) | MAE 折间± | RMSE (h) | R² | R² 折间± |\n"
             "|---|---|---|---|---|---|\n")
    for r in results:
        table += (f"| {r['model']} | {r['mae']:.2f} | ±{r['mae_std']:.2f} | "
                  f"{r['rmse']:.2f} | {r['r2']:+.4f} | ±{r['r2_std']:.4f} |\n")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("# 剩余寿命（RUL）回归结果\n\n")
        f.write(f"- 数据：`{os.path.basename(args.data)}`（{len(rows)} 条）\n")
        f.write(f"- 标签：`RUL_hours = (1 - severity) × {MAX_RUL_HOURS:.0f}`，"
                f"范围 {y.min():.1f} ~ {y.max():.1f} h\n")
        f.write(f"- 验证：{args.folds} 折交叉验证（shuffle，seed={args.seed}），"
                f"指标为全折汇总值 + 折间标准差\n")
        f.write(f"- 特征（{len(names)} 维）：{', '.join('`'+n+'`' for n in names)}\n\n")
        f.write("## 指标对比\n\n" + table + "\n")
        f.write("> **一定要有 Dummy 基线**：R² 若低于 0，说明模型还不如「永远预测平均值」。\n\n")
        f.write(f"**最优模型**：{best['model']}（MAE={best['mae']:.2f} h，RMSE={best['rmse']:.2f} h，"
                f"R²={best['r2']:.4f}）\n\n")
        f.write("## 结论\n\n")
        f.write(f"1. 学习型模型显著优于 Dummy 基线：R² 从 {dummy['r2']:+.4f} 提升到 {best['r2']:+.4f}，"
                f"MAE 从 {dummy['mae']:.2f} h 降到 {best['mae']:.2f} h。\n")
        f.write(f"2. 折间标准差很小（MAE ±{best['mae_std']:.2f} h），说明结果稳定，"
                f"不是某一次划分的运气。\n")
        f.write("3. 局限：标签由 `severity` 线性映射得到，属于**合成真值**；"
                "真实 RUL 需要失效时间，且健康样本是删失数据，应采用生存分析类方法。\n")

    html_path = os.path.splitext(args.out)[0] + "_pred_vs_actual.html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
                "<title>RUL 预测 vs 真实</title>"
                "<style>body{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;"
                "background:#f8f9fa;margin:0;padding:26px}.card{background:#fff;border:1px solid #e9ecef;"
                "border-radius:10px;padding:12px;margin-bottom:18px;max-width:640px}"
                "h1{font-size:20px;margin:0 0 4px}.meta{color:#868e96;font-size:13px;margin-bottom:18px}"
                "pre{background:#f8f9fa;padding:10px;border-radius:8px;font-size:12px;overflow:auto}"
                "</style></head><body><h1>剩余寿命预测：预测值 vs 真实值</h1>"
                f"<div class='meta'>{os.path.basename(args.data)} · {args.folds} 折交叉验证 · "
                f"红色虚线为理想线 y=x</div>")
        for name, _ in models:
            f.write(f"<div class='card'>{svg_scatter(y, preds[name], name)}</div>")
        f.write("</body></html>")

    # * B6 修复：models/ 在 .gitignore 里，
    # 全新 clone 没有该目录 → 原来 open() 会直接 FileNotFoundError [err]
    os.makedirs("models", exist_ok=True)
    with open("models/rul_model.json", "w", encoding="utf-8") as f:
        json.dump({"features": names, "max_rul_hours": MAX_RUL_HOURS,
                   "folds": args.folds, "results": results,
                   "best_model": best["model"]}, f, ensure_ascii=False, indent=2)

    print("  已写出 : " + os.path.abspath(args.out))
    print("  已写出 : " + os.path.abspath(html_path))
    print("  已写出 : " + os.path.abspath("models/rul_model.json"))


if __name__ == "__main__":
    main()
