#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Modbus TCP 采集器（纯标准库，手写协议）→ SQLite 落库
================================================================
职责：
    1. 周期轮询 Modbus 从站的保持寄存器（功能码 0x03）
    2. 按标定系数换算成工程量（℃ / mm/s / rpm / A）
    3. 写入 SQLite（后续可无缝换成 TDengine / InfluxDB）
    4. 断线自动重连（指数退避）+ 全流程日志 —— 这是工业现场最基本的要求

用法：
    python collector.py                              # 采 60 秒
    python collector.py --duration 20 --interval 1
    python collector.py --host 127.0.0.1 --port 5020 --unit 1
    python collector.py --db data/sensor.db

设计说明（面试可讲）：
    · 手写 MBAP 头 + PDU，不依赖 pymodbus，能讲清 Modbus TCP 报文结构
    · 事务号 txid 自增并校验回包，防止串包
    · SO_KEEPALIVE + 超时，避免半开连接把采集卡死
    · 重连采用指数退避（1→2→4→8 秒，上限 15 秒），避免风暴式重连
"""

import argparse
import datetime as dt
import os
import socket
import sqlite3
import struct
import sys
import time

# Windows 控制台默认是 GBK，直接打印 emoji/特殊中文会抛 UnicodeEncodeError 把进程搞崩。
# 统一把标准输出改成 UTF-8 + replace，保证任何终端下都不会因为日志字符而崩溃。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

FC_READ_HOLDING = 0x03

# 寄存器 → (列名, 标定系数, 单位)
REG_MAP = [
    ("temperature_c", 0.1, "℃"),
    ("vibration_mm_s", 0.01, "mm/s"),
    ("speed_rpm", 1.0, "rpm"),
    ("current_a", 0.01, "A"),
]
REG_START = 0
REG_QTY = len(REG_MAP)


# ---------------------------------------------------------------- Modbus 协议
def build_read_request(txid: int, unit_id: int, addr: int, qty: int) -> bytes:
    """构造 FC=0x03 请求：MBAP(7B) + PDU(5B)"""
    pdu = struct.pack(">BHH", FC_READ_HOLDING, addr, qty)
    mbap = struct.pack(">HHHB", txid & 0xFFFF, 0, len(pdu) + 1, unit_id)
    return mbap + pdu


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接被对端关闭")
        buf += chunk
    return buf


def parse_read_response(sock: socket.socket, expect_txid: int, qty: int):
    """解析 FC=0x03 响应，返回原始寄存器值列表"""
    header = recv_exact(sock, 7)
    txid, _pid, length, _unit = struct.unpack(">HHHB", header)
    # B4 修复（Codex task-002）：MBAP 的 length 非法时（0 或超上限），原来会
    # 走到 recv(-1) 或用过短 body 切片 → IndexError/struct.error 逃出重连循环崩进程
    if not (2 <= length <= 260):
        raise ValueError(f"非法 MBAP length={length}（合法范围 2~260）")
    body = recv_exact(sock, length - 1)
    if len(body) < 5:
        raise ValueError(f"响应 PDU 过短（{len(body)} 字节），无法解析")

    if txid != expect_txid:
        raise ValueError(f"事务号不匹配：期望 {expect_txid}，收到 {txid}（可能串包）")

    fc = body[0]
    if fc & 0x80:
        raise IOError(f"Modbus 异常响应，异常码 = {body[1]}")
    if fc != FC_READ_HOLDING:
        raise IOError(f"功能码异常：{fc}")
    byte_count = body[1]
    if byte_count != qty * 2:
        raise ValueError(f"字节数不符：{byte_count} != {qty * 2}")

    return [struct.unpack(">H", body[2 + i * 2: 4 + i * 2])[0] for i in range(qty)]


# ---------------------------------------------------------------- 数据库
def init_db(path: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    cols = ", ".join(f"{name} REAL" for name, _k, _u in REG_MAP)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS sensor_readings (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT    NOT NULL,
            epoch         REAL    NOT NULL,
            {cols},
            quality       TEXT    DEFAULT 'good'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON sensor_readings(ts)")
    conn.commit()
    return conn


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="Modbus TCP 采集器 → SQLite")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5020)
    ap.add_argument("--unit", type=int, default=1)
    ap.add_argument("--interval", type=float, default=1.0, help="采样间隔（秒）")
    ap.add_argument("--duration", type=float, default=60.0, help="采集时长（秒），0=一直采")
    ap.add_argument("--db", default="data/sensor.db")
    ap.add_argument("--log", default="logs/collector.log")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(os.path.abspath(args.log)), exist_ok=True)
    logf = open(args.log, "a", encoding="utf-8")

    def log(msg: str):
        line = f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    log("=" * 58)
    log(f"采集器启动 target={args.host}:{args.port} unit={args.unit} "
        f"interval={args.interval}s duration={args.duration}s db={args.db}")

    db = init_db(args.db)

    txid = 0
    ok, fail, reconnects = 0, 0, 0
    backoff = 1.0
    sock = None
    started = time.time()
    last_report = started

    def connect():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        s.settimeout(3.0)
        s.connect((args.host, args.port))
        return s

    try:
        while True:
            if args.duration and (time.time() - started) >= args.duration:
                break

            # ---- 未连接则尝试重连（指数退避）----
            if sock is None:
                try:
                    sock = connect()
                    log(f"✅ 已连接 Modbus 从站 {args.host}:{args.port}")
                    backoff = 1.0
                except OSError as e:
                    fail += 1
                    reconnects += 1
                    log(f"⚠️ 连接失败（{e.__class__.__name__}: {e}），{backoff:.0f} 秒后重试")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 15.0)
                    continue

            # ---- 采集一次 ----
            cycle_start = time.time()
            try:
                txid = (txid + 1) & 0xFFFF
                sock.sendall(build_read_request(txid, args.unit, REG_START, REG_QTY))
                raw = parse_read_response(sock, txid, REG_QTY)
                values = [r * k for r, (n, k, u) in zip(raw, REG_MAP)]
                now = dt.datetime.now()
                db.execute(
                    "INSERT INTO sensor_readings (ts, epoch, temperature_c, vibration_mm_s, speed_rpm, current_a, quality)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], time.time(), *values, "good"),
                )
                db.commit()
                ok += 1
                log("📥 采样 #{:<5} 温度={:5.1f}℃  振动={:5.2f}mm/s  转速={:6.0f}rpm  电流={:5.2f}A"
                    .format(ok, values[0], values[1], values[2], values[3]))
            except (OSError, ConnectionError, ValueError, IOError, struct.error, IndexError) as e:   # B4: 解析类异常也重连，别让采集进程崩掉
                fail += 1
                log(f"❌ 采集异常（{e.__class__.__name__}: {e}）→ 断开重连")
                try:
                    sock.close()
                except OSError:
                    pass
                sock = None

            # 固定频率：扣除本次耗时
            elapsed = time.time() - cycle_start
            time.sleep(max(0.0, args.interval - elapsed))

            # 每 30 秒打一次心跳
            if time.time() - last_report >= 30:
                last_report = time.time()
                log(f"📊 心跳：成功 {ok} 次 / 失败 {fail} 次 / 重连 {reconnects} 次")
    except KeyboardInterrupt:
        log("收到 Ctrl+C，正在退出…")
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass

    total = ok + fail
    rate = (ok / total * 100) if total else 0.0
    log(f"采集结束：成功 {ok} 次，失败 {fail} 次，重连 {reconnects} 次，成功率 {rate:.1f}%")
    log(f"数据已写入 {os.path.abspath(args.db)}")
    db.close()
    logf.close()

    # 控制台小结（便于截图为简历/README 佐证）
    print("\n" + "=" * 58)
    print(f"  成功采样 : {ok} 条")
    print(f"  失败次数 : {fail} 次")
    print(f"  重连次数 : {reconnects} 次")
    print(f"  成功率   : {rate:.1f}%")
    print(f"  数据库   : {os.path.abspath(args.db)}")
    print("=" * 58)


if __name__ == "__main__":
    main()
