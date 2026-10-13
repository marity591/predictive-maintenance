#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
薄时序库层（SQLite 实现）—— 降采样 / 保留策略 / 聚合查询
================================================================
为什么需要"时序库"这一层：
    采集端 1 秒 1 条，一天 86400 条，一年 3000 万条。直接把原始点全留着会有两个问题：
      ① 磁盘线性增长（写入放大、备份变慢）；
      ② 画一张"过去 7 天趋势图"要扫几百万行，查询越来越慢。
    工业界通行做法是 **原始短保留 + 聚合长保留**：
      · ts_raw  ：秒级原始点，只留最近 N 天（默认 7 天）
      · ts_1m   ：1 分钟聚合（avg/min/max/count/sum_sq），长期保留
    趋势图查聚合表，排查细节时再回查原始表 —— 这就是 InfluxDB/TDengine 的
    连续查询(continuous query)与保留策略(retention policy)的最小实现。

本模块也是"用得上真时序库之前"的过渡方案：SQLite 无服务、单文件、随处可跑。
表结构与查询语义刻意贴近时序库，后续换 TDengine/InfluxDB 时上层代码几乎不用改。

子命令：
    init                       建表
    seed-demo                  生成一段带劣化的历史数据（默认 24 小时 / 1 秒 1 条）
    rollup --interval 1min     把原始点聚合成 1 分钟桶（幂等，可重复跑）
    query  --metric ...        按分辨率查询（raw / 1m）
    stats                      看行数、时间跨度、磁盘占用、聚合压缩比
    retention --raw-days N     删除超期原始点（聚合数据保留）
    verify                     * 自检：造数 → 聚合 → 对比查询正确性与耗时
"""

import argparse
import math
import os
import random
import sqlite3
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

METRICS = ["temperature_c", "vibration_mm_s", "speed_rpm", "current_a"]
DEFAULT_DB = "data/tsdb.db"


# ---------------------------------------------------------------- 连接与建表
def connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")      # 时序写入场景：WAL 明显减少写阻塞
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS ts_raw (
        device        TEXT    NOT NULL,
        epoch         REAL    NOT NULL,
        ts            TEXT    NOT NULL,
        temperature_c REAL,
        vibration_mm_s REAL,
        speed_rpm     REAL,
        current_a     REAL,
        quality       TEXT    DEFAULT 'good',
        PRIMARY KEY (device, epoch)
    );
    CREATE INDEX IF NOT EXISTS idx_raw_epoch ON ts_raw(epoch);
    CREATE INDEX IF NOT EXISTS idx_raw_dev_epoch ON ts_raw(device, epoch);

    -- 聚合表用"长表"(metric 作为一行)：新增测点不用改表结构，这是时序库常见设计
    CREATE TABLE IF NOT EXISTS ts_agg (
        device       TEXT    NOT NULL,
        metric       TEXT    NOT NULL,
        bucket_epoch INTEGER NOT NULL,          -- 桶起点（秒）
        interval_s   INTEGER NOT NULL,          -- 桶宽（秒）
        avg          REAL,
        min          REAL,
        max          REAL,
        cnt          INTEGER,
        sum_sq       REAL,                      -- 存平方和 → 标准差可事后推导
        PRIMARY KEY (device, metric, interval_s, bucket_epoch)
    );
    CREATE INDEX IF NOT EXISTS idx_agg_lookup ON ts_agg(device, metric, interval_s, bucket_epoch);
    """)
    conn.commit()


# ---------------------------------------------------------------- 造历史数据
def seed_demo(conn, hours: float, interval_s: float, device: str, seed: int = 42):
    """生成带"健康→劣化"过程的秒级数据（用与 modbus_sim 一致的物理模型）"""
    rnd = random.Random(seed)
    n = int(hours * 3600 / interval_s)
    fault_start = int(n * 0.4)          # 前 40% 健康，之后开始劣化
    now = time.time()
    start = now - n * interval_s
    rows = []
    for i in range(n):
        t = i * interval_s
        sev = 0.0 if i < fault_start else min(1.0, (i - fault_start) / max(1, n - fault_start))
        temp = 28.5 + 1.2 * math.sin(t / 3700) + 12.0 * sev + rnd.gauss(0, 0.15)
        vib = (1.5 + 0.25 * abs(math.sin(t / 1100)) + 5.0 * sev
               + (rnd.gauss(0, 1.0) ** 2) * 2.2 * sev + abs(rnd.gauss(0, 0.05)))
        rpm = 1480 + 12 * math.sin(t / 2300) + rnd.gauss(0, 1.5)
        curr = 8.2 + 0.3 * math.sin(t / 1900) + 1.6 * sev + rnd.gauss(0, 0.03)
        ep = start + t
        rows.append((device, ep, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ep)),
                     temp, vib, rpm, curr, "good"))
    conn.executemany(
        "INSERT OR REPLACE INTO ts_raw (device,epoch,ts,temperature_c,vibration_mm_s,"
        "speed_rpm,current_a,quality) VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return n


# ---------------------------------------------------------------- 降采样
def rollup(conn, interval_s: int = 60, device: str = None, since: float = 0.0):
    """把原始点聚合成 interval_s 宽的桶；幂等（重跑只会覆盖同一批桶）

    * since 会被【向下对齐到桶边界】（Codex 在：
      否则增量聚合会用半个桶的统计覆盖掉整桶的聚合值 ——
      epoch 0..59 的桶先全量得到 cnt=60/avg=29.5，
      再 rollup(since=30) 就变成 cnt=30/avg=44.5（是数据错误，不是主键冲突）。
      对齐后每次都重算完整桶；已被 retention 删掉原始点的桶不会被本函数碰到。
    """
    if interval_s <= 0:
        raise ValueError("interval_s 必须为正数")
    since_bucket = math.floor(since / interval_s) * interval_s
    where = "WHERE epoch >= ?"
    params = [since_bucket]
    if device:
        where += " AND device = ?"
        params.append(device)

    # * A1 修复：原来是无条件 UPSERT ——
    #   当 retention 已删掉某历史桶的部分原始点后，再跑 rollup 会用【不完整】的
    #   COUNT/AVG 覆盖掉当初由完整数据算出的聚合值（实测 60/29.5/0/59 → 30/44.5/30/59）。
    #   现在只允许"原始点不少于已有聚合"时才覆盖（见下面的 WHERE 子句）：
    #     · 桶的原始点已被删掉一部分 → excluded.cnt < ts_agg.cnt → 保留历史聚合 [ok]
    #     · 桶有新到的原始点（cnt 变大）或数量相同（重算） → 正常更新 [ok]
    # * A/B 修复：用"删除水位"替代原来的 COUNT 守卫 ——
    #   · 桶起点 >= 水位：该桶的原始范围从未被删除过 → 自由重算（计数可升可降，合法更正也能生效 [ok]）
    #   · 桶起点 <  水位：与已删除范围相交，历史成员不可复现 → 【不参与本次重算】，已有聚合冻结 [ok]
    deleted_before = _deleted_before(conn)
    _iv = int(interval_s)
    _wl = f" CAST(epoch / {_iv} AS INTEGER) * {_iv} >= ?"
    where = (where + " AND" + _wl) if where.strip() else ("WHERE" + _wl)
    params.append(deleted_before)

    total = 0
    for m in METRICS:
        cur = conn.execute(f"""
            INSERT INTO ts_agg (device, metric, bucket_epoch, interval_s, avg, min, max, cnt, sum_sq)
            SELECT device, ?, CAST(epoch / ? AS INTEGER) * ?, ?, AVG({m}), MIN({m}), MAX({m}),
                   COUNT({m}), SUM({m}*{m})
            FROM ts_raw {where}
            GROUP BY device, CAST(epoch / ? AS INTEGER)
            ON CONFLICT(device, metric, interval_s, bucket_epoch) DO UPDATE SET
                avg = excluded.avg, min = excluded.min, max = excluded.max,
                cnt = excluded.cnt, sum_sq = excluded.sum_sq
        """, [m, interval_s, interval_s, interval_s, *params, interval_s])
        total += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    conn.commit()
    return total


def std_from(sum_sq, avg, cnt):
    # 已知数值局限：这是平方和相减式公式，
    # 在【均值大、方差极小】时会发生灾难性抵消 ——
    # 60 个 1480±1e-5 的样本真值 σ≈1e-5，本函数返回 0（100% 误差）。
    # 当前 demo 最低桶 σ≈1.06 rpm、最大误差仅 3.5e-10，尚不影响使用；
    # 将来要高精度应改存稳定的中心二阶矩（Welford M2）。
    if not cnt or cnt < 2 or sum_sq is None or avg is None:
        return 0.0
    var = max(0.0, sum_sq / cnt - avg * avg)
    return math.sqrt(var)


# ---------------------------------------------------------------- 查询
def query_raw(conn, metric, device, start, end):
    if metric not in METRICS:
        raise ValueError(f"未知测点 {metric}")
    return conn.execute(
        f"SELECT epoch, {metric} AS value FROM ts_raw WHERE device=? AND epoch>=? AND epoch<? ORDER BY epoch",
        (device, start, end)).fetchall()


def query_agg(conn, metric, device, start, end, interval_s=60):
    """按【与窗口相交的完整桶】契约返回聚合行。

    · 下界：floor(start/interval)*interval —— 返回所有与 [start,end) 相交的桶
    · 上界：ceil(end/interval)*interval（排他）—— end 正好在桶边界时不含该桶
    · end <= start：返回空（原来空窗口仍返回一个桶，与 query_raw 的半开语义不一致）
    [warn] 相交桶可能含窗口外的样本；需要严格窗口统计时应回查 ts_raw。
    """
    if end <= start:
        return []
    lo = math.floor(start / interval_s) * interval_s
    # * A3 修复：原来用 int(end) 截断，end=60.1 会算成 60、
    #   漏掉本应与窗口相交的桶 60；而 time.time() 本来就返回小数，这不是理论边界。
    hi = int(math.ceil(end / interval_s)) * interval_s   # 相交桶：向上取整到桶边界
    return conn.execute(
        "SELECT bucket_epoch, avg, min, max, cnt, sum_sq FROM ts_agg "
        "WHERE device=? AND metric=? AND interval_s=? AND bucket_epoch>=? AND bucket_epoch<? "
        "ORDER BY bucket_epoch",
        (device, metric, interval_s, lo, hi)).fetchall()


# ---------------------------------------------------------------- 删除水位（A/B 修复）
def _deleted_before(conn):
    """原始点【删除水位】：epoch 小于它的原始点曾被 retention 不可恢复地删除过。

    * A/B 修复：不能用 COUNT 判断"桶是否完整" ——
      ① 数量回升 ≠ 成员完整：删掉旧点再补入同数新点，COUNT 会伪装成"完整"[err]
      ② COUNT 守卫还会挡住合法更正：某点被更正为 NULL 会让有效计数下降 [err]
    水位是唯一可靠的信号：它只在真正发生删除时前进，且与删除同事务落盘 [ok]
    """
    conn.execute("CREATE TABLE IF NOT EXISTS ts_meta (key TEXT PRIMARY KEY, value REAL)")
    row = conn.execute("SELECT value FROM ts_meta WHERE key='raw_deleted_before'").fetchone()
    if row is not None:
        return float(row[0])
    # *
    #   那样一个"既有库"会被当成"从未删除过"，再次用不完整原始集合覆盖历史聚合 [err]
    #   区分两种情况：
    #     · 新库（ts_agg 为空）→ 确实没删除过 → 0.0 安全 [ok]
    #     · 既有库（ts_agg 非空）→ 历史删除情况【未知】→ 明确报错，要求人工声明截点或重建 [ok]
    agg_rows = conn.execute("SELECT COUNT(*) FROM ts_agg").fetchone()[0]
    if agg_rows == 0:
        # 新库：确认"从未删除过"，并把 0.0 持久化 ——
        # * 否则第一次 rollup 产生聚合后，第二次调用会把它误判成"既有库"而报错 [err]
        conn.execute("INSERT OR IGNORE INTO ts_meta(key, value) VALUES('raw_deleted_before', 0.0)")
        return 0.0
    raise RuntimeError(
        "检测到【既有数据库】缺少删除水位记录（ts_meta.raw_deleted_before）：\n"
        "  该库已有 %d 行聚合，但历史删除情况未知，无法保证聚合完整性 [err]\n"
        "  请二选一（不要用当前数量当作完整性证据）：\n"
        "    ① 显式声明历史删除截点：\n"
        "       python -c \"import tsdb; c=tsdb.connect('data/tsdb.db'); "
        "print(tsdb.mark_deleted_before(c, <截点epoch>))\"\n"
        "    ② 从可信归档重建：重新 init 并重跑 rollup（旧聚合会被丢弃，需先备份）\n"
        "  说明：水位表示『epoch 小于它的原始点曾被不可恢复地删除』；\n"
        "        若该库从未跑过 retention，可用 0.0 显式声明。" % agg_rows)


def mark_deleted_before(conn, cutoff):
    """【迁移助手】显式声明历史删除截点（供既有库升级使用）。

    只应在你确知"epoch 小于 cutoff 的原始点已被不可恢复地删除"时调用；
    若该库从未跑过 retention，用 0.0 显式声明即可 [ok]
    """
    new = _set_deleted_before(conn, float(cutoff))
    conn.commit()
    return new


def _set_deleted_before(conn, cutoff):
    conn.execute("CREATE TABLE IF NOT EXISTS ts_meta (key TEXT PRIMARY KEY, value REAL)")
    # * 注意：这里必须【直读原始行】，不能调用带守卫的 _deleted_before() ——
    #   否则迁移函数会被自己的守卫挡住（既有库正因缺记录才需要迁移）[err]
    row = conn.execute("SELECT value FROM ts_meta WHERE key='raw_deleted_before'").fetchone()
    old_v = float(row[0]) if row is not None else 0.0
    new = max(old_v, float(cutoff))
    conn.execute("INSERT INTO ts_meta(key, value) VALUES('raw_deleted_before', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (new,))
    return new


# ---------------------------------------------------------------- 保留策略
def retention(conn, raw_days: float, dry_run=False):
    cutoff = time.time() - raw_days * 86400
    n = conn.execute("SELECT COUNT(*) FROM ts_raw WHERE epoch < ?", (cutoff,)).fetchone()[0]
    if not dry_run:
        conn.execute("DELETE FROM ts_raw WHERE epoch < ?", (cutoff,))
        _set_deleted_before(conn, cutoff)      # * 与删除【同事务】落盘（不 commit 两次）
        conn.commit()
    return n, cutoff


# ---------------------------------------------------------------- stats
def stats(conn, db_path):
    raw = conn.execute("SELECT COUNT(*), MIN(epoch), MAX(epoch) FROM ts_raw").fetchone()
    agg = conn.execute("SELECT COUNT(*), COUNT(DISTINCT metric) FROM ts_agg").fetchone()
    size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    out = {
        "raw_rows": raw[0], "raw_from": raw[1], "raw_to": raw[2],
        "agg_rows": agg[0], "agg_metrics": agg[1], "db_bytes": size,
        "raw_deleted_before": _deleted_before(conn),   # A/B：删除水位
    }
    if out["raw_rows"] and out["agg_rows"]:
        out["ratio"] = out["raw_rows"] / out["agg_rows"]
    return out


def fmt_ts(ep):
    return "—" if not ep else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ep))


def fmt_num(v, digits=4):
    """B8 修复：整桶缺测时 avg/min/max 是 NULL，
    原来 f"{fmt_num(row['avg'])}" 会对 None 抛 TypeError [err] 统一走这里，输出 —"""
    return "—" if v is None else f"{v:.{digits}f}"


# ---------------------------------------------------------------- 命令实现
def cmd_stats(conn, args):
    s = stats(conn, args.db)
    print("=" * 66)
    print(f"  时序库 : {os.path.abspath(args.db)}")
    print(f"  原始点 : {s['raw_rows']} 行   {fmt_ts(s['raw_from'])} → {fmt_ts(s['raw_to'])}")
    print(f"  聚合点 : {s['agg_rows']} 行（{s['agg_metrics']} 个测点）")
    print(f"  磁盘   : {s['db_bytes']/1024:.1f} KB")
    if s.get("ratio"):
        print(f"  压缩比 : 原始点在聚合后减少为 1/{s['ratio']:.1f}（趋势图只需扫聚合表）")
    print("=" * 66)


def cmd_verify(conn, args):
    """自检：造数 → 聚合 → 验证聚合值与原始数据一致 + 对比查询耗时"""
    print("[verify] 1) 造 24 小时 / 1 秒数据 …")
    conn.execute("DELETE FROM ts_raw")           # 幂等：先清空
    conn.execute("DELETE FROM ts_agg")
    conn.commit()
    n = seed_demo(conn, hours=24, interval_s=1, device="line1")
    print(f"  [verify]    写入 {n} 行原始点")

    print("[verify] 2) 降采样为 1 分钟桶 …")
    t0 = time.perf_counter()
    rollup(conn, interval_s=60)
    t_roll = time.perf_counter() - t0
    s = stats(conn, args.db)
    print(f"  [verify]    rollup 用时 {t_roll:.2f}s，聚合表 {s['agg_rows']} 行，"
          f"压缩比 1/{s.get('ratio', 0):.1f}")

    print("[verify] 3) 抽查一个桶，核对 avg/min/max 是否与原始数据一致 …")
    row = conn.execute(
        "SELECT bucket_epoch, avg, min, max, cnt, sum_sq FROM ts_agg "
        "WHERE device='line1' AND metric='vibration_mm_s' ORDER BY bucket_epoch LIMIT 1").fetchone()
    b = row["bucket_epoch"]
    raw = conn.execute(
        "SELECT AVG(vibration_mm_s), MIN(vibration_mm_s), MAX(vibration_mm_s), COUNT(*) "
        "FROM ts_raw WHERE device='line1' AND epoch >= ? AND epoch < ?",
        (b, b + 60)).fetchone()
    ok = (abs(row["avg"] - raw[0]) < 1e-9 and abs(row["min"] - raw[1]) < 1e-9
          and abs(row["max"] - raw[2]) < 1e-9 and row["cnt"] == raw[3])
    print(f"  [verify]    桶 @ {fmt_ts(b)}  cnt={row['cnt']}  "
          f"avg={fmt_num(row['avg'])} (raw {fmt_num(raw[0])})  "
          f"min={fmt_num(row['min'])} (raw {fmt_num(raw[1])})  max={fmt_num(row['max'])} (raw {fmt_num(raw[2])})")
    print(f"  [verify]    一致性校验: {'[ok] 通过' if ok else '❌ 不一致'}")
    print(f"  [verify]    推导标准差 = {std_from(row['sum_sq'], row['avg'], row['cnt']):.4f}")

    print("[verify] 4) 对比查询耗时：原始表 vs 聚合表（查最近 6 小时振动）")
    end = conn.execute("SELECT MAX(epoch) FROM ts_raw").fetchone()[0]
    start = end - 6 * 3600
    t0 = time.perf_counter()
    r1 = query_raw(conn, "vibration_mm_s", "line1", start, end)
    t_raw = time.perf_counter() - t0
    t0 = time.perf_counter()
    r2 = query_agg(conn, "vibration_mm_s", "line1", start, end, 60)
    t_agg = time.perf_counter() - t0
    print(f"  [verify]    原始表: {len(r1):>6} 点，用时 {t_raw*1000:7.1f} ms")
    print(f"  [verify]    聚合表: {len(r2):>6} 点，用时 {t_agg*1000:7.1f} ms"
          f"   → 点数减少 {len(r1)/max(1,len(r2)):.0f} 倍，"
          f"耗时降低 {(1 - t_agg/max(t_raw,1e-9))*100:.1f}%")
    print("[verify] 5) 保留策略演练（删除 1 天前的原始点，聚合保留）")
    gone, cutoff = retention(conn, raw_days=1, dry_run=True)
    print(f"  [verify]    将会删除 {gone} 行原始点（cutoff={fmt_ts(cutoff)}），聚合表不受影响")
    print("=" * 66)
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="薄时序库层（降采样 / 保留策略 / 聚合查询）")
    ap.add_argument("--db", default=DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    p = sub.add_parser("seed-demo"); p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--interval", type=float, default=1.0); p.add_argument("--device", default="line1")
    p = sub.add_parser("rollup"); p.add_argument("--interval", type=int, default=60)
    p.add_argument("--device"); p.add_argument("--since", type=float, default=0.0)
    p = sub.add_parser("query"); p.add_argument("--metric", default="vibration_mm_s")
    p.add_argument("--device", default="line1"); p.add_argument("--resolution", choices=["raw", "1m"], default="1m")
    p.add_argument("--last", type=float, default=3600, help="查询最近多少秒")
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("retention"); p.add_argument("--raw-days", type=float, default=7.0)
    p.add_argument("--dry-run", action="store_true")
    sub.add_parser("stats")
    sub.add_parser("verify")

    args = ap.parse_args()
    conn = connect(args.db)
    init_db(conn)

    if args.cmd == "init":
        print(f"  [tsdb] 已初始化 {os.path.abspath(args.db)}")
    elif args.cmd == "seed-demo":
        n = seed_demo(conn, args.hours, args.interval, args.device)
        print(f"  [tsdb] 已写入 {n} 行原始点（{args.hours} 小时 / {args.interval}s）→ {os.path.abspath(args.db)}")
    elif args.cmd == "rollup":
        t0 = time.perf_counter()
        rollup(conn, args.interval, args.device, args.since)
        print(f"  [tsdb] 降采样完成（桶宽 {args.interval}s），用时 {time.perf_counter()-t0:.2f}s")
        cmd_stats(conn, args)
    elif args.cmd == "query":
        end = time.time() + 1
        start = end - args.last
        if args.resolution == "raw":
            rows = query_raw(conn, args.metric, args.device, start, end)
            print(f"  [tsdb] raw  {len(rows)} 点（最近 {args.last:.0f}s）")
            for r in rows[-args.limit:]:
                print(f"   {fmt_ts(r['epoch'])}  {fmt_num(r['value'])}")
        else:
            rows = query_agg(conn, args.metric, args.device, start, end, 60)
            print(f"  [tsdb] 1m   {len(rows)} 桶（最近 {args.last:.0f}s）")
            for r in rows[-args.limit:]:
                print(f"   {fmt_ts(r['bucket_epoch'])}  avg={fmt_num(r['avg'])} "
                      f"min={fmt_num(r['min'])} max={fmt_num(r['max'])} cnt={r['cnt']} "
                      f"std={fmt_num(std_from(r['sum_sq'], r['avg'], r['cnt']))}")
    elif args.cmd == "retention":
        gone, cutoff = retention(conn, args.raw_days, args.dry_run)
        print(f"  [tsdb] {'将' if args.dry_run else '已'}删除 {gone} 行原始点"
              f"（早于 {fmt_ts(cutoff)}）；聚合数据不受影响")
    elif args.cmd == "stats":
        cmd_stats(conn, args)
    elif args.cmd == "verify":
        sys.exit(cmd_verify(conn, args))

    conn.close()


if __name__ == "__main__":
    main()
