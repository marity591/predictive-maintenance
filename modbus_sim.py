#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Modbus TCP 从站（Slave）模拟器 —— 纯标准库实现，无需 pymodbus
================================================================
用途：在没有真实 PLC / 传感器的情况下，模拟一台工业设备对外提供数据，
 让采集程序、时序库、看板、AI 模型都能先跑起来。

模拟 4 个保持寄存器（Holding Register，功能码 0x03）：
 地址 0 温度 (int16, 单位 0.1 ℃) 例 285 = 28.5 ℃
 地址 1 振动 (int16, 单位 0.01 mm/s) 例 152 = 1.52 mm/s
 地址 2 转速 (int16, 单位 1 rpm) 例 1480 = 1480 rpm
 地址 3 电流 (int16, 单位 0.01 A) 例 823 = 8.23 A

特点：
 · 数据带趋势 + 噪声 + 偶发尖峰，后面做特征工程 / 异常检测 / 寿命预测才有信号
 · 支持"故障注入"：--fault-after 秒后开始劣化（振动爬升、温度上升），
 便于演示"异常告警"与"剩余寿命预测"

用法：
 python modbus_sim.py # 监听 127.0.0.1:5020
 python modbus_sim.py --port 5020 --unit 1
 python modbus_sim.py --fault-after 60 # 60 秒后开始劣化
"""

import argparse
import math
import random
import socket
import struct
import sys
import threading
import time

# Windows 控制台默认是 GBK，直接打印 emoji/特殊中文会抛 UnicodeEncodeError 把进程搞崩。
# 统一把标准输出改成 UTF-8 + replace，保证任何终端下都不会因为日志字符而崩溃。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REG_TEMP, REG_VIB, REG_RPM, REG_CURR = 0, 1, 2, 3
REG_COUNT = 16

# ---- Modbus 功能码 ----
FC_READ_HOLDING = 0x03
EXC_ILLEGAL_FUNCTION = 0x01
EXC_ILLEGAL_ADDRESS = 0x02


class DeviceSimulator:
    """一台被模拟的旋转设备（电机 + 轴承）"""

    def __init__(self, fault_after: float = 0.0):
        self.t = 0.0
        self.fault_after = fault_after
        self.lock = threading.Lock()
        self.regs = [0] * REG_COUNT
        self._update()

 # 劣化程度 0.0 ~ 1.0（用于演示异常/寿命预测）
    @property
    def degradation(self) -> float:
        if self.fault_after <= 0 or self.t < self.fault_after:
            return 0.0
        return min(1.0, (self.t - self.fault_after) / 120.0)

    def tick(self, dt: float = 1.0):
        self.t += dt
        self._update()

    def _update(self):
        d = self.degradation
 # 温度：随负载波动 + 劣化时上升（28.5 ℃ → 最高约 41 ℃）
        temp = 28.5 + 1.2 * math.sin(self.t / 37.0) + 12.0 * d + random.gauss(0, 0.15)
 # 振动：基础很小，劣化时明显爬升（1.5 → 6.5 mm/s）
        vib = 1.5 + 0.25 * abs(math.sin(self.t / 11.0)) + 5.0 * d + abs(random.gauss(0, 0.05))
 # 转速：约 1480 rpm
        rpm = 1480 + 12 * math.sin(self.t / 23.0) + random.gauss(0, 1.5)
 # 电流：约 8.2 A，劣化时上升
        curr = 8.2 + 0.3 * math.sin(self.t / 19.0) + 1.6 * d + random.gauss(0, 0.03)

        with self.lock:
            self.regs[REG_TEMP] = int(round(temp * 10))
            self.regs[REG_VIB] = int(round(vib * 100))
            self.regs[REG_RPM] = int(round(rpm))
            self.regs[REG_CURR] = int(round(curr * 100))

    def read(self, addr: int, qty: int):
        with self.lock:
            if addr < 0 or qty <= 0 or addr + qty > REG_COUNT:
                return None
            return list(self.regs[addr:addr + qty])


def recv_exact(conn: socket.socket, n: int) -> bytes:
    """读满 n 字节（TCP 是流，必须循环读）"""
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return b""
        buf += chunk
    return buf


def handle_client(conn: socket.socket, addr, sim: DeviceSimulator, unit_id: int, verbose: bool):
    conn.settimeout(30.0)
    try:
        while True:
            header = recv_exact(conn, 7)          # MBAP: txid(2) pid(2) len(2) unit(1)
            if not header:
                return
            txid, pid, length, unit = struct.unpack(">HHHB", header)
            body = recv_exact(conn, length - 1)   # PDU
            if not body:
                return

            fc = body[0]
            if fc != FC_READ_HOLDING:
                resp_pdu = struct.pack(">BB", fc | 0x80, EXC_ILLEGAL_FUNCTION)
            else:
                start, qty = struct.unpack(">HH", body[1:5])
                vals = sim.read(start, qty)
                if vals is None:
                    resp_pdu = struct.pack(">BB", fc | 0x80, EXC_ILLEGAL_ADDRESS)
                else:
                    payload = b"".join(struct.pack(">H", v & 0xFFFF) for v in vals)
                    resp_pdu = struct.pack(">BB", fc, len(payload)) + payload
                    if verbose:
                        print(f"[SIM] 读寄存器 addr={start} qty={qty} -> {vals}", flush=True)

            resp = struct.pack(">HHHB", txid, pid, len(resp_pdu) + 1, unit_id) + resp_pdu
            conn.sendall(resp)
    except (socket.timeout, ConnectionResetError, OSError):
        return
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description="Modbus TCP 从站模拟器（纯标准库）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5020, help="默认 5020（1024 以上无需管理员权限）")
    ap.add_argument("--unit", type=int, default=1, help="从站号 / Unit ID")
    ap.add_argument("--fault-after", type=float, default=0.0, help="多少秒后开始故障劣化（0=不劣化）")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    sim = DeviceSimulator(fault_after=args.fault_after)

 # 后台推进模拟时间
    def ticker():
        while True:
            time.sleep(1.0)
            sim.tick(1.0)

    threading.Thread(target=ticker, daemon=True).start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(8)

    print("=" * 62)
    print("  Modbus TCP 从站模拟器已启动")
    print(f"  监听      : {args.host}:{args.port}   Unit ID = {args.unit}")
    print("  寄存器    : 0=温度(0.1℃) 1=振动(0.01mm/s) 2=转速(rpm) 3=电流(0.01A)")
    print(f"  故障注入  : {'关闭' if args.fault_after <= 0 else str(args.fault_after) + ' 秒后开始劣化'}")
    print("  停止      : Ctrl+C")
    print("=" * 62, flush=True)

    try:
        while True:
            conn, addr = srv.accept()
            if args.verbose:
                print(f"[SIM] 客户端接入 {addr}", flush=True)
            threading.Thread(target=handle_client,
                             args=(conn, addr, sim, args.unit, args.verbose),
                             daemon=True).start()
    except KeyboardInterrupt:
        print("\n[SIM] 已停止")
    finally:
        srv.close()


if __name__ == "__main__":
    main()
