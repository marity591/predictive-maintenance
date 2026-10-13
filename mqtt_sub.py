#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MQTT 订阅端（纯标准库）—— 观察上行链路是否真的通了
================================================================
用途：
 · 本地自测：配合 mqtt_bridge.py --builtin 看数据有没有发出来
 · 真实环境：连 EMQX / Mosquitto 当"云侧消费者"的最简原型

用法：
 python mqtt_sub.py --port 18830 --topic 'factory/+/telemetry' --count 5 --timeout 20
 python mqtt_sub.py --host 127.0.0.1 --port 1883 --topic '#' --count 1
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mqtt_min import MqttClient  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="MQTT 订阅端（纯标准库）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--topic", default="factory/+/telemetry")
    ap.add_argument("--count", type=int, default=0, help="收到多少条后退出（0=一直收）")
    ap.add_argument("--timeout", type=float, default=30.0, help="整体超时（秒）")
    ap.add_argument("--client-id", default="py-sub")
    args = ap.parse_args()

    c = MqttClient(host=args.host, port=args.port, client_id=args.client_id)
    rc = c.connect()
    print(f"[sub] CONNECT ok (rc={rc}) → {args.host}:{args.port}  订阅 {args.topic}")
    c.subscribe(args.topic)

    started, got = time.time(), 0
    try:
        while time.time() - started < args.timeout:
            msg = c.loop(timeout=2)
            if not msg:
                continue
            topic, payload = msg
            got += 1
            text = payload.decode("utf-8", "replace")
            try:
                obj = json.loads(text)
                m = obj.get("metrics", {})
                print(f"[sub] #{got} {topic}  seq={obj.get('seq')}  "
                      f"vib={m.get('vibration_mm_s')}  temp={m.get('temperature_c')}")
            except json.JSONDecodeError:
                print(f"[sub] #{got} {topic}  {text[:120]}")
            if args.count and got >= args.count:
                break
    except KeyboardInterrupt:
        pass
    finally:
        c.disconnect()

    print("-" * 56)
    print(f"[sub] 共收到 {got} 条消息")
    if got == 0:
        print("[sub] [warn] 一条都没收到：检查 Broker 是否在跑、主题是否匹配、桥接是否已启动")
        sys.exit(2)


if __name__ == "__main__":
    main()
