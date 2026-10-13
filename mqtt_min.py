#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MQTT 3.1.1 最小实现（纯标准库）—— 报文编解码 + 可直接用于测试的迷你 Broker
================================================================
为什么要手写：
    1. 工业现场经常要在**没有第三方库**的环境（PLC 网关、受限镜像）里发 MQTT，
       理解报文结构才能自己实现或用极简的依赖；
    2. 面试里"MQTT 报文长什么样、QoS 怎么保证"是很常见的追问点，
       自己写过一遍就答得出来；
    3. 手写一个迷你 Broker 后，本项目的 MQTT 链路可以**完全离线自测**，
       不必先装 EMQX/Mosquitto。

实现的报文（MQTT 3.1.1）：
    连接类：CONNECT(1) / CONNACK(2) / PINGREQ(12) / PINGRESP(13) / DISCONNECT(14)
    发布类：PUBLISH(3) / PUBACK(4)（QoS1）
    订阅类：SUBSCRIBE(8) / SUBACK(9)

报文结构回顾：
    ┌──────── 固定头（2~5 字节）────────┐┌──── 可变头 + 载荷 ────┐
    │ byte1: 类型(4bit) + 标志(4bit)     ││ 取决于报文类型          │
    │ 剩余长度: 变长整数（1~4 字节）      ││                        │
    └───────────────────────────────────┘└────────────────────────┘
    剩余长度用"每字节低 7 位为数据、最高位为续接标志"编码，最多 4 字节（最大 256MB）
"""

import socket
import struct
import threading
import time

# ---------------------------------------------------------------- 报文类型
CONNECT, CONNACK = 1, 2
PUBLISH, PUBACK = 3, 4
SUBSCRIBE, SUBACK = 8, 9
PINGREQ, PINGRESP = 12, 13
DISCONNECT = 14


# ---------------------------------------------------------------- 基础编码
def encode_remaining_length(n: int) -> bytes:
    """剩余长度：变长整数编码"""
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n > 0:
            b |= 0x80
        out.append(b)
        if n == 0:
            break
    return bytes(out)


def encode_string(s: str) -> bytes:
    """UTF-8 字符串：2 字节长度 + 内容"""
    data = s.encode("utf-8")
    return struct.pack(">H", len(data)) + data


def build_packet(pkt_type: int, flags: int, payload: bytes) -> bytes:
    return bytes([(pkt_type << 4) | (flags & 0x0F)]) + encode_remaining_length(len(payload)) + payload


def build_connect(client_id: str, keepalive: int = 60, clean_session: bool = True) -> bytes:
    # 可变头: 协议名"MQTT" + 协议级别 4 + 连接标志 + 保活时间
    flags = 0x02 if clean_session else 0x00
    var = encode_string("MQTT") + bytes([4, flags]) + struct.pack(">H", keepalive)
    return build_packet(CONNECT, 0, var + encode_string(client_id))


def build_publish(topic: str, payload: bytes, qos: int = 0, retain: bool = False,
                  packet_id: int = 0) -> bytes:
    flags = (0x01 if retain else 0x00) | ((qos & 0x03) << 1)
    var = encode_string(topic)
    if qos > 0:
        var += struct.pack(">H", packet_id)
    return build_packet(PUBLISH, flags, var + payload)


def build_subscribe(topic_filter: str, qos: int, packet_id: int) -> bytes:
    return build_packet(SUBSCRIBE, 0x02, struct.pack(">H", packet_id) + encode_string(topic_filter) + bytes([qos]))


def build_puback(packet_id: int) -> bytes:
    return build_packet(PUBACK, 0, struct.pack(">H", packet_id))


def build_pingreq() -> bytes:
    return build_packet(PINGREQ, 0, b"")


def build_disconnect() -> bytes:
    return build_packet(DISCONNECT, 0, b"")


# ---------------------------------------------------------------- 解码
class Reader:
    """从字节流里按需读取，便于解析可变长报文"""

    def __init__(self, buf: bytes):
        self.buf = buf
        self.i = 0

    def u8(self) -> int:
        v = self.buf[self.i]
        self.i += 1
        return v

    def u16(self) -> int:
        v = struct.unpack(">H", self.buf[self.i:self.i + 2])[0]
        self.i += 2
        return v

    def string(self) -> str:
        n = self.u16()
        s = self.buf[self.i:self.i + n].decode("utf-8", "replace")
        self.i += n
        return s

    def rest(self) -> bytes:
        v = self.buf[self.i:]
        self.i = len(self.buf)
        return v


def recv_packet(sock: socket.socket, timeout: float = 10.0):
    """读取一个完整 MQTT 报文，返回 (类型, 标志, 载荷字节) 或 None（对端关闭）"""
    sock.settimeout(timeout)
    head = sock.recv(1)
    if not head:
        return None
    pkt_type, flags = head[0] >> 4, head[0] & 0x0F

    # 变长"剩余长度"
    multiplier, length = 1, 0
    while True:
        b = sock.recv(1)
        if not b:
            return None
        byte = b[0]
        length += (byte & 0x7F) * multiplier
        if not (byte & 0x80):
            break
        multiplier *= 128
        if multiplier > 128 ** 3:
            raise ValueError("剩余长度编码非法")

    payload = b""
    while len(payload) < length:
        chunk = sock.recv(length - len(payload))
        if not chunk:
            return None
        payload += chunk
    return pkt_type, flags, payload


def topic_matches(topic_filter: str, topic: str) -> bool:
    """MQTT 主题匹配：+ 匹配单层，# 匹配多层（必须位于末尾）"""
    f, t = topic_filter.split("/"), topic.split("/")
    for i, part in enumerate(f):
        if part == "#":
            return True
        if i >= len(t):
            return False
        if part != "+" and part != t[i]:
            return False
    return len(f) == len(t)


# ---------------------------------------------------------------- 迷你 Broker
class MiniBroker:
    """仅用于本地自测的极简 Broker：支持 CONNECT/PUBLISH(QoS0,1)/SUBSCRIBE/PING/DISCONNECT

    生产环境当然要用 EMQX / Mosquitto —— 这里的目标是"让链路离线可验证"。
    """

    def __init__(self, host="127.0.0.1", port=1883):
        self.host, self.port = host, port
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((host, port))
        self.srv.listen(16)
        self.clients = []          # [(sock, subscriptions)]
        self.lock = threading.Lock()
        self.running = True
        self.received = []         # [(topic, payload)] 便于测试断言
        self.stats = {"connect": 0, "publish": 0, "subscribe": 0}

    def start(self):
        threading.Thread(target=self._accept_loop, daemon=True).start()
        return self

    def stop(self):
        self.running = False
        try:
            self.srv.close()
        except OSError:
            pass

    def _accept_loop(self):
        while self.running:
            try:
                conn, _addr = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._client_loop, args=(conn,), daemon=True).start()

    def _client_loop(self, conn: socket.socket):
        subs = []
        try:
            while self.running:
                pkt = recv_packet(conn, timeout=120)
                if pkt is None:
                    return
                pkt_type, flags, payload = pkt

                if pkt_type == CONNECT:
                    self.stats["connect"] += 1
                    conn.sendall(build_packet(CONNACK, 0, bytes([0, 0])))   # 接受连接
                    with self.lock:
                        self.clients.append([conn, subs])

                elif pkt_type == PUBLISH:
                    qos = (flags >> 1) & 0x03
                    r = Reader(payload)
                    topic = r.string()
                    packet_id = r.u16() if qos > 0 else 0
                    data = r.rest()
                    self.stats["publish"] += 1
                    self.received.append((topic, data))
                    # 分发给订阅者
                    with self.lock:
                        targets = [(s, list(sub)) for s, sub in self.clients]
                    for sock, sub_list in targets:
                        for f in sub_list:
                            if topic_matches(f, topic):
                                try:
                                    sock.sendall(build_publish(topic, data, qos=0))
                                except OSError:
                                    pass
                                break
                    if qos == 1 and packet_id:
                        conn.sendall(build_puback(packet_id))

                elif pkt_type == SUBSCRIBE:
                    r = Reader(payload)
                    packet_id = r.u16()
                    while r.i < len(r.buf):
                        f = r.string()
                        qos = r.u8()
                        subs.append(f)
                        self.stats["subscribe"] += 1
                    conn.sendall(build_packet(SUBACK, 0, struct.pack(">H", packet_id) + bytes([0] * len(subs))))

                elif pkt_type == PINGREQ:
                    conn.sendall(build_packet(PINGRESP, 0, b""))

                elif pkt_type == DISCONNECT:
                    return
        except (OSError, ValueError):
            return
        finally:
            with self.lock:
                self.clients = [c for c in self.clients if c[0] is not conn]
            try:
                conn.close()
            except OSError:
                pass


# ---------------------------------------------------------------- 简单客户端
class MqttClient:
    """够用的客户端：connect / publish / subscribe / loop / disconnect"""

    def __init__(self, host="127.0.0.1", port=1883, client_id="py-client", keepalive=60):
        self.host, self.port = host, port
        self.client_id, self.keepalive = client_id, keepalive
        self.sock = None
        self.packet_id = 0
        self.on_message = None
        self._stop = False

    def connect(self, timeout=5.0):
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.sendall(build_connect(self.client_id, self.keepalive))
        pkt = recv_packet(self.sock, timeout=timeout)
        if not pkt or pkt[0] != CONNACK:
            raise ConnectionError("CONNACK 未收到")
        return pkt[2][1] if len(pkt[2]) > 1 else 0    # 返回码

    def subscribe(self, topic_filter: str, qos: int = 0):
        self.packet_id = (self.packet_id + 1) & 0xFFFF or 1
        self.sock.sendall(build_subscribe(topic_filter, qos, self.packet_id))
        pkt = recv_packet(self.sock, timeout=5)
        if not pkt or pkt[0] != SUBACK:
            raise ConnectionError("SUBACK 未收到")

    def publish(self, topic: str, payload, qos: int = 0, retain: bool = False):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        pid = 0
        if qos > 0:
            self.packet_id = (self.packet_id + 1) & 0xFFFF or 1
            pid = self.packet_id
        self.sock.sendall(build_publish(topic, payload, qos=qos, retain=retain, packet_id=pid))
        if qos == 1:
            pkt = recv_packet(self.sock, timeout=5)
            if not pkt or pkt[0] != PUBACK:
                raise ConnectionError("PUBACK 未收到")

    def loop(self, timeout: float = 1.0):
        """收一轮消息；返回 (topic, payload) 或 None

        注意：**读超时是正常情况**（这段时间没有消息），必须当成"本轮无消息"返回，
        不能让它抛 socket.timeout 冒到调用方 —— 这是本实现踩过的坑：
        订阅端第一次 2 秒没收到消息就整个线程崩了，日志里只剩一条 CONNECT。
        """
        while not self._stop:
            try:
                pkt = recv_packet(self.sock, timeout=timeout)
            except (socket.timeout, TimeoutError):
                return None
            except OSError:
                return None
            if pkt is None:
                return None
            pkt_type, _flags, payload = pkt
            if pkt_type == PUBLISH:
                r = Reader(payload)
                topic = r.string()
                return topic, r.rest()
            if pkt_type == PINGRESP:
                continue
        return None

    def ping(self):
        self.sock.sendall(build_pingreq())
        pkt = recv_packet(self.sock, timeout=5)
        return bool(pkt and pkt[0] == PINGRESP)

    def disconnect(self):
        try:
            self.sock.sendall(build_disconnect())
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


if __name__ == "__main__":
    # 自测：起 Broker → 订阅 → 发布 → 收到
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    broker = MiniBroker(port=18830).start()
    time.sleep(0.3)

    sub = MqttClient(port=18830, client_id="tester-sub")
    sub.connect()
    sub.subscribe("factory/+/telemetry")

    pub = MqttClient(port=18830, client_id="tester-pub")
    pub.connect()
    pub.publish("factory/line1/telemetry", '{"vibration":1.23}', qos=1)

    msg = sub.loop(timeout=3)
    print("收到:", msg)
    assert msg and msg[0] == "factory/line1/telemetry", "订阅/发布回路失败"
    print("stats:", broker.stats)
    print("[ok] MQTT 编解码 + 迷你 Broker 自测通过")
    sub.disconnect(); pub.disconnect(); broker.stop()
