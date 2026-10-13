#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MQTT 上行桥接（存储转发 + 持久化游标）—— 把 SQLite 里的遥测数据发到 MQTT Broker
================================================================
为什么不是"采集器直接发 MQTT"：
    工业现场网络并不总是可靠（4G 抖动、厂区 Wi-Fi 掉线）。如果采集进程直接发 MQTT，
    断网期间的数据就丢了。正确做法是 **本地先落库、再异步上行**（store-and-forward）：
        · 断网时：数据留在 SQLite，发送游标不越过未确认行
        · 恢复后：从上次游标继续补发
        · **进程重启**：游标写在磁盘上，重启后继续（而不是从头重发）

投递语义（面试考点）：
    QoS1 下收到客户端的 PUBACK 成功返回后才保存游标；未确认行会先重试。
    至少一次补发依赖原始库/游标完好、单一桥接写者及最终恢复连接；不承诺端到端不丢。
    QoS0 只表示写入 socket，不能保证 Broker 收到。确认丢失或保存游标前退出可能重复；
    消费端须用 device + seq 去重，Broker 持久化和消费端处理另行保证。

主题设计：
    factory/<device>/telemetry     遥测数据（JSON）
    factory/<device>/status        连接/状态（启动与退出各发一条）

用法：
    # ① 不要外部 Broker，就地起一个内置迷你 Broker 自测
    python mqtt_bridge.py --builtin --topic-device line1 --max 5

    # ② 连真实 Broker（EMQX / Mosquitto），持续跟随
    python mqtt_bridge.py --host 127.0.0.1 --port 1883 --topic-device line1 --follow

    # ③ 只看会发什么、不真的连
    python mqtt_bridge.py --dry-run --max 3

    # ④ 想从头重发（忽略已保存的游标）
    python mqtt_bridge.py --builtin --reset-cursor
"""

import argparse
import json
import os
import socketserver  # noqa: F401  (保留导入位置，说明本文件只用标准库)
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mqtt_min import MiniBroker, MqttClient  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

COLS = ["temperature_c", "vibration_mm_s", "speed_rpm", "current_a"]


# ---------------------------------------------------------------- 游标持久化
def cursor_path(device: str) -> str:
    return os.path.join("data", f"mqtt_cursor_{device}.json")


def load_cursor(device: str) -> int:
    p = cursor_path(device)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return int(json.load(f).get("last_id", 0))
        except (ValueError, OSError):
            return 0
    return 0


def save_cursor(device: str, last_id: int):
    p = cursor_path(device)
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"last_id": last_id, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
    os.replace(tmp, p)          # 原子替换，避免写一半断电导致游标损坏


# ---------------------------------------------------------------- 数据读取
def fetch_rows(db_path, after_id, limit):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT id, ts, {', '.join(COLS)} FROM sensor_readings WHERE id > ? ORDER BY id LIMIT ?",
        (after_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def to_payload(row):
    return json.dumps({
        "ts": row["ts"],
        "seq": row["id"],                                    # 供消费端幂等去重
        "device": None,                                      # 由上层填充（占位）
        "metrics": {c: round(row[c], 4) for c in COLS},
        "quality": "good",
    }, ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser(description="SQLite → MQTT 存储转发桥接（持久化游标）")
    ap.add_argument("--db", default="data/sensor.db")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--topic-device", default="line1")
    ap.add_argument("--qos", type=int, default=1, choices=[0, 1])
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--max", type=int, default=0, help="最多发送多少条后退出（0=不限）")
    ap.add_argument("--follow", action="store_true", help="持续跟随（默认发完现有数据就退出）")
    ap.add_argument("--builtin", action="store_true", help="就地启动内置迷你 Broker（自测用）")
    ap.add_argument("--dry-run", action="store_true", help="只打印不外发")
    ap.add_argument("--reset-cursor", action="store_true", help="忽略已保存游标，从头开始")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"[ERROR] 找不到 {args.db}（先跑 collector.py 或 run_demo.ps1）")
        sys.exit(1)

    # ---------------- 游标 ----------------
    last_id = 0 if args.reset_cursor else load_cursor(args.topic_device)
    if last_id:
        print(f"[bridge] 从磁盘游标恢复：last_id=#{last_id}"
              f"（{cursor_path(args.topic_device)}）→ 断网/重启期间的数据将补发")
    else:
        print("[bridge] 游标为 0，从最早的数据开始发送")

    # ---------------- Broker / 客户端 ----------------
    broker, port = None, args.port
    if args.builtin and not args.dry_run:
        port = args.port if args.port != 1883 else 18830
        broker = MiniBroker(port=port).start()
        time.sleep(0.3)
        print(f"[mqtt] 内置迷你 Broker 已启动 127.0.0.1:{port}")

    client = None
    if not args.dry_run:
        client = MqttClient(host=args.host, port=port, client_id=f"bridge-{args.topic_device}")
        rc = client.connect()
        if rc != 0:
            client.disconnect()
            raise ConnectionError(f"Broker 拒绝连接：return code={rc}")
        print(f"[mqtt] CONNECT 成功（return code={rc}）→ {args.host}:{port}")

    topic = f"factory/{args.topic_device}/telemetry"
    status_topic = f"factory/{args.topic_device}/status"
    if client:
        try:
            client.publish(status_topic, json.dumps({"state": "online", "ts": time.time()}), qos=0)
        except Exception:
            pass

    sent, failed = 0, 0
    started = time.time()
    print(f"[bridge] 目标主题 {topic}  qos={args.qos}  "
          f"{'（dry-run，不外发）' if args.dry_run else ''}")

    try:
        while True:
            rows = fetch_rows(args.db, last_id, args.batch)
            if not rows:
                if not args.follow:
                    break
                time.sleep(args.interval)
                continue

            for row in rows:
                payload = to_payload(row)
                payload = payload.replace('"device": null', f'"device": "{args.topic_device}"')
                if args.dry_run:
                    print(f"  → 预览 #{row['id']} {payload}")
                    last_id, sent = row["id"], sent + 1
                else:
                    # 当前行确认前，不离开内层循环；重连失败也不能跳到下一行。
                    while True:
                        try:
                            if client is None:
                                client = MqttClient(host=args.host, port=port,
                                                    client_id=f"bridge-{args.topic_device}")
                                rc = client.connect()
                                if rc != 0:
                                    raise ConnectionError(f"Broker 拒绝连接：return code={rc}")
                                print(f"  🔄 已重连，重试 #{row['id']}")
                            client.publish(topic, payload, qos=args.qos)
                        except OSError as e:
                            failed += 1
                            print(f"  ⚠️ #{row['id']} 未确认（{e.__class__.__name__}: {e}）—— "
                                  f"游标仍为 #{last_id}，暂不发送后续行")
                            if client:
                                try:
                                    client.disconnect()
                                except Exception:
                                    pass
                            client = None
                            time.sleep(min(2 ** min(failed, 4), 15))
                            continue
                        break
                    # 落盘失败直接报错退出，不重发后继续推进；重启可能重复当前行。
                    save_cursor(args.topic_device, row["id"])
                    last_id = row["id"]
                    sent += 1
                    if sent % 10 == 0 or sent <= 3:
                        result = "客户端确认成功" if args.qos == 1 else "已写入 socket（未确认送达）"
                        print(f"  ✅ #{row['id']} {result}: vib={row['vibration_mm_s']:.2f} "
                              f"temp={row['temperature_c']:.1f}")
                if args.max and sent >= args.max:
                    break
            if args.max and sent >= args.max:
                break
    except KeyboardInterrupt:
        print("\n[bridge] 收到 Ctrl+C")
    finally:
        if client:
            try:
                client.publish(status_topic, json.dumps({"state": "offline", "sent": sent}), qos=0)
            except Exception:
                pass
            try:
                client.disconnect()
            except Exception:
                pass
        if broker:
            print(f"[mqtt] 内置 Broker 统计: {broker.stats}")
            broker.stop()

    elapsed = max(1e-6, time.time() - started)
    print("-" * 62)
    action = "预览" if args.dry_run else ("客户端确认成功" if args.qos == 1 else "写入 socket")
    print(f"[bridge] {action} {sent} 条，失败 {failed} 次，用时 {elapsed:.1f}s，"
          f"平均 {sent/elapsed:.2f} 条/秒")
    if args.dry_run:
        print("[bridge] dry-run 未外发，未写持久游标")
    else:
        print(f"[bridge] 持久游标 = #{load_cursor(args.topic_device)} → 下次从后续行补发")


if __name__ == "__main__":
    main()
