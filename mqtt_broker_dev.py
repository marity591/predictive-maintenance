#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地开发用迷你 MQTT Broker（纯标准库）
================================================================
用途：在没有 EMQX / Mosquitto 的机器上，快速起一个能收发的 Broker，
      用于联调 `mqtt_bridge.py` / `mqtt_sub.py`。

注意：这是**开发/自测工具**，不是生产 Broker：
      · 单进程、内存态、无持久化、无认证、无 QoS2
      · 生产请用 EMQX / Mosquitto / HiveMQ

用法：
    python mqtt_broker_dev.py                       # 监听 1883，一直运行
    python mqtt_broker_dev.py --port 18830 --seconds 60
    python mqtt_broker_dev.py --port 18830 --dump-json

配套（三个终端）：
    终端1  python mqtt_broker_dev.py --port 18830 --seconds 120
    终端2  python mqtt_sub.py  --port 18830 --topic 'factory/+/telemetry'
    终端3  python mqtt_bridge.py --port 18830 --follow   # 连这个 Broker
"""

import argparse
import json
import sys
import time

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from mqtt_min import MiniBroker  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="本地开发用迷你 MQTT Broker（纯标准库）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883, help="默认 1883；自测建议 18830")
    ap.add_argument("--seconds", type=float, default=0, help="运行多少秒后退出（0=一直运行）")
    ap.add_argument("--interval", type=float, default=5.0, help="打印统计的间隔（秒）")
    ap.add_argument("--dump-json", action="store_true", help="退出时把收到的消息以 JSON 打印")
    args = ap.parse_args()

    b = MiniBroker(host=args.host, port=args.port).start()
    print(f"[broker] 迷你 MQTT Broker 已启动 → {args.host}:{args.port}", flush=True)
    print("[broker] 停止：Ctrl+C；这是开发用 Broker，请勿用于生产", flush=True)

    started = time.time()
    try:
        while True:
            time.sleep(args.interval)
            print(f"[broker] stats={b.stats}  已收消息={len(b.received)}  "
                  f"在线客户端={len(b.clients)}", flush=True)
            if args.seconds and (time.time() - started) >= args.seconds:
                break
    except KeyboardInterrupt:
        print("\n[broker] 收到 Ctrl+C", flush=True)
    finally:
        if args.dump_json and b.received:
            print(json.dumps([{"topic": t, "payload": p.decode("utf-8", "replace")}
                              for t, p in b.received], ensure_ascii=False, indent=2))
        print(f"[broker] 最终统计: {b.stats}  共收 {len(b.received)} 条消息", flush=True)
        b.stop()


if __name__ == "__main__":
    main()
