#!/usr/bin/env python3
"""把 collector 的追加式 sensor_readings 显式导入 ts_raw（只用标准库）。

源库只读；原始工程量、epoch、ts、quality 原样保留。目标按 device+epoch
UPSERT，导入游标与数据在同一事务提交。源库不得原地替换、重编号或修订旧行；
每个源路径/device 对应一个追加式采集流。迁移源路径应保留旧库并先完成导入。
"""
import argparse
import math
import os
from pathlib import Path
import sqlite3

import tsdb


def ingest(source, target, device="line1", batch_size=1000):
    if not device or batch_size <= 0:
        raise ValueError("device 不能为空，batch_size 必须为正数")
    # 使用稳定的绝对路径，不依赖 Windows realpath 的额外目录句柄权限。
    source_path = Path(os.path.abspath(source))
    if not source_path.is_file():
        raise FileNotFoundError(f"源库不存在：{source_path}")
    if (os.path.normcase(str(source_path)) == os.path.normcase(os.path.abspath(target))
            or (Path(target).exists() and os.path.samefile(source_path, target))):
        raise ValueError("源库与目标时序库必须不同")
    # mode=ro 避免源路径写错时悄悄创建空库。
    src = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    dst = None
    try:
        src.execute("BEGIN")  # 固定快照；采集中新增的行留给下一轮。
        high = src.execute("SELECT COALESCE(MAX(id), 0) FROM sensor_readings").fetchone()[0]
        dst = tsdb.connect(str(target))
        tsdb.init_db(dst)
        dst.execute("""CREATE TABLE IF NOT EXISTS ingest_progress (
            source TEXT NOT NULL, device TEXT NOT NULL, last_id INTEGER NOT NULL,
            PRIMARY KEY (source, device))""")
        source_key = os.path.normcase(str(source_path))
        imported = 0
        with dst:
            dst.execute("BEGIN IMMEDIATE")
            previous = dst.execute(
                "SELECT last_id FROM ingest_progress WHERE source=? AND device=?",
                (source_key, device)).fetchone()
            last_id = previous[0] if previous else 0
            if high < last_id:
                raise ValueError("源库 id 回退：请先检查源库是否被替换/清空，禁止自动重置游标")
            while last_id < high:
                rows = src.execute("""SELECT id, epoch, ts, temperature_c, vibration_mm_s,
                    speed_rpm, current_a, quality FROM sensor_readings
                    WHERE id > ? AND id <= ? ORDER BY id LIMIT ?""",
                    (last_id, high, batch_size)).fetchall()
                if not rows:
                    break
                for row in rows:
                    if row["epoch"] is None or not math.isfinite(row["epoch"]):
                        raise ValueError(f"sensor_readings id={row['id']} 的 epoch 非有限数")
                dst.executemany("""INSERT INTO ts_raw
                    (device, epoch, ts, temperature_c, vibration_mm_s, speed_rpm, current_a, quality)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(device, epoch) DO UPDATE SET
                        ts=excluded.ts, temperature_c=excluded.temperature_c,
                        vibration_mm_s=excluded.vibration_mm_s, speed_rpm=excluded.speed_rpm,
                        current_a=excluded.current_a, quality=excluded.quality""",
                    [(device, r["epoch"], r["ts"], r["temperature_c"], r["vibration_mm_s"],
                      r["speed_rpm"], r["current_a"], r["quality"]) for r in rows])
                last_id = rows[-1]["id"]
                imported += len(rows)
            dst.execute("""INSERT INTO ingest_progress(source, device, last_id) VALUES (?, ?, ?)
                ON CONFLICT(source, device) DO UPDATE SET last_id=excluded.last_id""",
                (source_key, device, last_id))
        return imported, last_id
    finally:
        src.close()
        if dst is not None:
            dst.close()


def main():
    ap = argparse.ArgumentParser(description="collector sensor_readings → tsdb ts_raw 幂等导入")
    ap.add_argument("--source", default="data/sensor.db")
    ap.add_argument("--db", default="data/tsdb.db")
    ap.add_argument("--device", default="line1")
    ap.add_argument("--batch", type=int, default=1000)
    args = ap.parse_args()
    try:
        imported, last_id = ingest(args.source, args.db, args.device, args.batch)
    except (OSError, sqlite3.Error, ValueError) as exc:
        ap.exit(1, f"[ingest] 失败，导入数据/游标事务未提交：{exc}\n")
    print(f"[ingest] 提交 {imported} 条源记录，device={args.device}，last_id={last_id}")


if __name__ == "__main__":
    main()
