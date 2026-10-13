"""task-003 regression tests; stdlib only.

Run: python -B -m unittest discover -s tests -v
P0_REPO selects an old checkout for before/after comparison.
P0_TEST_WORK optionally confines all generated fixtures to a chosen directory.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
REPO = Path(os.environ.get("P0_REPO", Path(__file__).resolve().parents[1])).resolve()
sys.path.insert(0, str(REPO))
import mqtt_bridge as bridge
import tsdb


@contextlib.contextmanager
def db_connection(path):
    # sqlite3's transaction context does not close handles (important on Windows).
    with contextlib.closing(sqlite3.connect(path)) as conn:
        with conn:
            yield conn


def load_ingest():
    spec = importlib.util.spec_from_file_location("ingest_under_test", REPO / "ingest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("P0_TEST_WORK"))
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.old_cwd = Path.cwd()
        os.chdir(self.work)
        self.addCleanup(os.chdir, self.old_cwd)

    def source(self, values=None):
        path = self.work / "sensor.db"
        with db_connection(path) as conn:
            conn.execute("""CREATE TABLE sensor_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, epoch REAL NOT NULL,
                temperature_c REAL, vibration_mm_s REAL, speed_rpm REAL,
                current_a REAL, quality TEXT DEFAULT 'good')""")
            conn.executemany("INSERT INTO sensor_readings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             values or [(i, f"t+{i}", 1700000040.0+i, 30., 1.5, 1480., 8.2, "good")
                                        for i in (1, 2, 3)])
        return path

    def cli(self, script, *args):
        result = subprocess.run([sys.executable, "-B", str(REPO / script), *map(str, args)],
                                cwd=self.work, capture_output=True, text=True,
                                encoding="utf-8", timeout=15,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        print("COMMAND", script, *args)
        print("EXIT", result.returncode)
        print(result.stdout, end="")
        print(result.stderr, end="")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result


class BridgeTests(Fixture):
    def client(self, fail_first=False, reconnect_failures=0, permanent=False, ack_lost=False):
        state = {"attempts": [], "delivered": [], "connects": 0}

        class Client:
            def __init__(self, **kwargs):
                pass

            def connect(self):
                state["connects"] += 1
                if 1 < state["connects"] <= 1 + reconnect_failures:
                    raise OSError("injected reconnect failure")
                return 0

            def publish(self, topic, payload, qos=0):
                if not topic.endswith("/telemetry"):
                    return
                seq = json.loads(payload)["seq"]
                state["attempts"].append(seq)
                if permanent:
                    if len(state["attempts"]) >= 3:
                        raise KeyboardInterrupt
                    raise OSError("injected persistent outage")
                if (fail_first or ack_lost) and len(state["attempts"]) == 1:
                    if ack_lost:
                        state["delivered"].append(seq)
                    raise OSError("injected lost ACK" if ack_lost else "injected failure before receive")
                state["delivered"].append(seq)

            def disconnect(self):
                pass

        return Client, state

    def run_bridge(self, client, *args):
        output = io.StringIO()
        with patch.object(bridge, "MqttClient", client), \
             patch.object(bridge, "time", types.SimpleNamespace(time=__import__('time').time,
                                                                strftime=__import__('time').strftime,
                                                                sleep=lambda _: None)), \
             patch.object(sys, "argv", ["mqtt_bridge.py", "--db", str(self.work / "sensor.db"),
                                        "--topic-device", "test", *args]), \
             contextlib.redirect_stdout(output):
            bridge.main()
        print(output.getvalue(), end="")
        return output.getvalue()

    def test_b1_failed_first_row_is_retried_before_later_rows(self):
        self.source()
        client, state = self.client(fail_first=True)
        self.run_bridge(client)
        print("B1 ACTUAL", state, "cursor", bridge.load_cursor("test"))
        self.assertEqual(state["attempts"], [1, 1, 2, 3])
        self.assertEqual(state["delivered"], [1, 2, 3])
        self.assertEqual(bridge.load_cursor("test"), 3)
        self.assertEqual(bridge.fetch_rows(str(self.work / "sensor.db"), 3, 50), [])

    def test_reconnect_failure_stays_on_same_row(self):
        self.source()
        client, state = self.client(fail_first=True, reconnect_failures=2)
        self.run_bridge(client, "--max", "1")
        self.assertEqual(state["attempts"], [1, 1])
        self.assertEqual(state["connects"], 4)
        self.assertEqual(state["delivered"], [1])
        self.assertEqual(bridge.load_cursor("test"), 1)

    def test_permanent_failure_interrupt_does_not_skip_row(self):
        self.source()
        client, state = self.client(permanent=True)
        self.run_bridge(client)
        self.assertEqual(state["attempts"], [1, 1, 1])
        self.assertEqual(bridge.load_cursor("test"), 0)

    def test_lost_ack_allows_duplicate_but_no_omission(self):
        self.source()
        client, state = self.client(ack_lost=True)
        self.run_bridge(client)
        self.assertEqual(state["delivered"], [1, 1, 2, 3])
        self.assertEqual(bridge.load_cursor("test"), 3)

    def test_cursor_save_failure_stops_before_next_row(self):
        self.source()
        client, state = self.client()
        with patch.object(bridge, "save_cursor", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.run_bridge(client)
        self.assertEqual(state["delivered"], [1])
        self.assertEqual(bridge.load_cursor("test"), 0)

    def test_b2_preview_does_not_advance_persistent_cursor(self):
        self.source()
        client, _ = self.client()
        preview = self.run_bridge(client, "--dry-run", "--max", "1")
        saved = bridge.load_cursor("test")
        client, state = self.client()
        self.run_bridge(client)
        print("B2 ACTUAL cursor_after_preview", saved, "real_delivered", state["delivered"])
        self.assertEqual(saved, 0)
        self.assertEqual(state["delivered"], [1, 2, 3])
        self.assertNotIn("已保存", preview)
        self.assertNotIn("上行 1 条", preview)
        self.assertIn("预览", preview)

    def test_reset_preview_preserves_existing_cursor_bytes(self):
        self.source()
        bridge.save_cursor("test", 2)
        before = Path(bridge.cursor_path("test")).read_bytes()
        client, _ = self.client()
        self.run_bridge(client, "--dry-run", "--reset-cursor", "--max", "1")
        self.assertEqual(Path(bridge.cursor_path("test")).read_bytes(), before)


class IngestTests(Fixture):
    def test_b7_collector_to_ingest_to_rollup_and_replay(self):
        # A real loopback TCP peer returns one Modbus frame; original collector main runs.
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)
        self.addCleanup(listener.close)
        errors = []

        def serve():
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(5)
                    request = b""
                    while len(request) < 12:
                        part = connection.recv(12 - len(request))
                        if not part:
                            raise RuntimeError("incomplete request")
                        request += part
                    txid = struct.unpack(">H", request[:2])[0]
                    body = bytes([3, 8]) + struct.pack(">4H", 300, 150, 1480, 820)
                    connection.sendall(struct.pack(">HHHB", txid, 0, len(body)+1, request[6]) + body)
            except Exception as exc:
                errors.append(str(exc))

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        source, target = self.work / "sensor.db", self.work / "tsdb.db"
        self.cli("collector.py", "--host", "127.0.0.1", "--port", listener.getsockname()[1],
                 "--duration", "0.2", "--interval", "0.3", "--db", source, "--log", self.work / "collector.log")
        thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        with db_connection(source) as conn:
            sample = conn.execute("SELECT epoch, ts, temperature_c, vibration_mm_s, speed_rpm, current_a, quality FROM sensor_readings").fetchall()
        self.assertEqual(len(sample), 1)
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        if (REPO / "ingest.py").exists():
            self.cli("ingest.py", "--source", source, "--db", target, "--device", "line1")
            self.cli("ingest.py", "--source", source, "--db", target, "--device", "line1")
        self.cli("tsdb.py", "--db", target, "rollup", "--interval", "60")
        with db_connection(target) as conn:
            actual = conn.execute("SELECT epoch, ts, temperature_c, vibration_mm_s, speed_rpm, current_a, quality FROM ts_raw WHERE device='line1'").fetchall()
            buckets = conn.execute("SELECT metric, cnt, avg FROM ts_agg ORDER BY metric").fetchall()
        print("B7 ACTUAL source_rows", len(sample), "raw_rows", len(actual), "buckets", buckets)
        self.assertEqual(actual, sample)
        self.assertEqual(len(buckets), 4)
        self.assertTrue(all(row[1] == 1 for row in buckets))
        self.assertEqual(dict((r[0], r[2]) for r in buckets),
                         {"temperature_c": 30., "vibration_mm_s": 1.5, "speed_rpm": 1480., "current_a": 8.2})
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_quality_null_values_duplicate_epoch_and_incremental_checkpoint(self):
        source = self.source([(1, "t", 1700000040., None, 2.5, 1500., 9., "suspect"),
                              (2, "t", 1700000040., None, 3., 1500., 9., "bad")])
        target = self.work / "tsdb.db"
        adapter = load_ingest()
        self.assertEqual(adapter.ingest(source, target, "line1", 1), (2, 2))
        self.assertEqual(adapter.ingest(source, target, "line1", 1), (0, 2))
        with db_connection(target) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ts_raw").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT temperature_c,vibration_mm_s,quality FROM ts_raw").fetchone(),
                             (None, 3., "bad"))
            conn.execute("DELETE FROM ts_raw")
        # Retention must not be undone by replaying already imported source IDs.
        self.assertEqual(adapter.ingest(source, target), (0, 2))
        with db_connection(target) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ts_raw").fetchone()[0], 0)
        self.assertEqual(adapter.ingest(source, target, "line2"), (2, 2))

    def test_bad_second_batch_rolls_back_data_and_checkpoint(self):
        source = self.source([(1, "t", 1., 30., 1., 1500., 8., "good"),
                              (2, "t", float("inf"), 30., 2., 1500., 8., "good")])
        target = self.work / "tsdb.db"
        adapter = load_ingest()
        with self.assertRaises(ValueError):
            adapter.ingest(source, target, batch_size=1)
        with db_connection(target) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ts_raw").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ingest_progress").fetchone()[0], 0)
        with db_connection(source) as conn:
            conn.execute("UPDATE sensor_readings SET epoch=2 WHERE id=2")
        self.assertEqual(adapter.ingest(source, target), (2, 2))

    def test_missing_source_is_not_created(self):
        source = self.work / "missing.db"
        with self.assertRaises(FileNotFoundError):
            load_ingest().ingest(source, self.work / "tsdb.db")
        self.assertFalse(source.exists())


class DocumentationTests(unittest.TestCase):
    longMessage = False

