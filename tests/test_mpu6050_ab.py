from __future__ import annotations

import json
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hitl_bridge.link import Deframer, encode_envelope

from experiments.mpu6050_ab import (
    Mpu6050ABParameters,
    Sample,
    _expected_burst,
    run,
)
from flatsat_controller.bench import Bench
from flatsat_controller.runtime import Operator, RunContext
from flatsat_controller.simulation import SimulationLink, SimulationLinkConfig


class FakeProcessGroup:
    def run(self, *args, **kwargs):
        raise AssertionError("process execution is not used")

    def close(self):
        return []


def _prefix(destination: int, source: int, kind: int) -> bytes:
    return struct.pack("<BBBQQB", 3, destination, source, time.time_ns(), 0, kind)


def _status(sequence: int, count: int, dropped: int = 0) -> bytes:
    body = struct.pack("<HIIIII", 1 if sequence != 0xffffffff else 0, sequence, count, dropped, 0, 0)
    return _prefix(253, 1, 3) + bytes((4,)) + body


def _result(request_id: int) -> bytes:
    return _prefix(253, 1, 3) + bytes((3,)) + struct.pack("<IBB", request_id, 2, 1)


def _transaction(transaction_id: int, sequence: int, burst: bytes, flags: int = 0) -> bytes:
    body = struct.pack("<I Q I I H B B", transaction_id, time.time_ns(), 0, 100, flags, 0, 0)
    body += bytes((0x3b, 14)) + burst
    body += struct.pack("<IHH", sequence, 0, 14)
    return _prefix(253, 1, 3) + bytes((2,)) + body


class FakeBridge:
    def __init__(self, *, mismatch=False, drop=False, counter_only=False, gap=False,
                 other_host=False, omit_second=False, tail_drop=False,
                 tail_late=False, tail_before_status=False, old_flagged=False,
                 wrap_ids=False):
        self.client, self.server = socket.socketpair()
        self.port = 4242
        self.mismatch = mismatch
        self.drop = drop
        self.counter_only = counter_only
        self.gap = gap
        self.other_host = other_host
        self.omit_second = omit_second
        self.tail_drop = tail_drop
        self.tail_late = tail_late
        self.tail_before_status = tail_before_status
        self.old_flagged = old_flagged
        self.baseline_count = 0xfffffffe if wrap_ids else (5 if old_flagged else 0)
        self.samples: list[int] = []
        self.sent_frames = 0
        self.errors: list[BaseException] = []
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _send_fragmented(self, conn: socket.socket, frame: bytes) -> None:
        self.sent_frames += 1
        packet = encode_envelope(frame)
        for offset in range(0, len(packet), 3):
            conn.sendall(packet[offset:offset + 3])

    def _run(self):
        try:
            conn = self.server
            with conn:
                conn.settimeout(2)
                deframer = Deframer()
                active = 0xffffffff
                count = self.baseline_count
                status_requests = 0
                request_buffer = bytearray()
                while True:
                    data = conn.recv(4096)
                    if not data:
                        break
                    for frame in deframer.push(data):
                        if frame[19] == 2 and frame[20] == 1:
                            active = int.from_bytes(frame[21:25], "little")
                            self.samples.append(active)
                            if self.omit_second and len(self.samples) == 2:
                                continue
                            values = (Sample(1, (0.0, 0.0, 9.80665), (0.0, 0.0, 0.0), 25.0),
                                      Sample(2, (1.0, -1.0, 9.80665), (0.1, -0.2, 0.3), 30.0))
                            index = self.samples.index(active)
                            burst = _expected_burst(values[index], Mpu6050ABParameters(values[0], values[1], 2, 250, 0.2))
                            if self.mismatch and index == 0:
                                burst = bytes((burst[0] ^ 1,)) + burst[1:]
                            count = (count + 1) & 0xffffffff
                            if self.other_host:
                                wrong_destination = bytearray(_transaction(count, active, burst))
                                wrong_destination[1] = 252
                                self._send_fragmented(conn, bytes(wrong_destination))
                            transaction_id = (
                                (count * 2 - 1) & 0xffffffff
                                if self.gap and count > 1
                                else count
                            )
                            flags = 1 if self.drop and not self.counter_only else 0
                            self._send_fragmented(conn, _transaction(transaction_id, active, burst, flags))
                        elif frame[19] == 1 and frame[20] == 2:
                            request_id = int.from_bytes(frame[21:25], "little")
                            status_requests += 1
                            final_tail = status_requests == 4 and (
                                self.tail_drop or self.tail_late or self.tail_before_status
                            )
                            if final_tail:
                                count = (count + 1) & 0xffffffff
                            if final_tail and self.tail_before_status:
                                values = (
                                    Sample(1, (0.0, 0.0, 9.80665), (0.0, 0.0, 0.0), 25.0),
                                    Sample(2, (1.0, -1.0, 9.80665), (0.1, -0.2, 0.3), 30.0),
                                )
                                burst = _expected_burst(
                                    values[1],
                                    Mpu6050ABParameters(values[0], values[1], 2, 250, 0.2),
                                )
                                self._send_fragmented(
                                    conn,
                                    _transaction(count, self.samples[-1], burst),
                                )
                            self._send_fragmented(conn, _status(active, count, int((self.drop or self.counter_only) and count > 0)))
                            self._send_fragmented(conn, _result(request_id))
                            if self.old_flagged and status_requests == 1:
                                self._send_fragmented(
                                    conn,
                                    _transaction(
                                        self.baseline_count,
                                        0xffffffff,
                                        bytes(14),
                                        flags=1,
                                    ),
                                )
                            if final_tail and self.tail_late:
                                values = (
                                    Sample(1, (0.0, 0.0, 9.80665), (0.0, 0.0, 0.0), 25.0),
                                    Sample(2, (1.0, -1.0, 9.80665), (0.1, -0.2, 0.3), 30.0),
                                )
                                burst = _expected_burst(
                                    values[1],
                                    Mpu6050ABParameters(values[0], values[1], 2, 250, 0.2),
                                )
                                self._send_fragmented(
                                    conn,
                                    _transaction(count, self.samples[-1], burst),
                                )
        except (OSError, TimeoutError) as exc:
            self.errors.append(exc)
        finally:
            self.server.close()

    def close(self):
        try:
            self.server.close()
        except OSError:
            pass
        self.thread.join(2)


class Mpu6050ABTests(unittest.TestCase):
    def _run(self, *, mismatch=False, drop=False, counter_only=False, gap=False,
             other_host=False, omit_second=False, tail_drop=False,
             tail_late=False, tail_before_status=False, old_flagged=False,
             wrap_ids=False):
        root_ctx = tempfile.TemporaryDirectory()
        self.addCleanup(root_ctx.cleanup)
        root = Path(root_ctx.name)
        run_dir = root / "run"
        run_dir.mkdir()
        bridge = FakeBridge(mismatch=mismatch, drop=drop, counter_only=counter_only, gap=gap,
                            other_host=other_host, omit_second=omit_second,
                            tail_drop=tail_drop, tail_late=tail_late,
                            tail_before_status=tail_before_status,
                            old_flagged=old_flagged, wrap_ids=wrap_ids)
        self.addCleanup(bridge.close)
        runtime = RunContext(run_dir, FakeProcessGroup())
        cfg = SimulationLinkConfig(host="127.0.0.1", port=bridge.port, io_timeout_s=0.05)
        bench = Bench(Operator(runtime), simulation_link=SimulationLink(cfg, runtime))
        a = Sample(11, (0.0, 0.0, 9.80665), (0.0, 0.0, 0.0), 25.0)
        b = Sample(12, (1.0, -1.0, 9.80665), (0.1, -0.2, 0.3), 30.0)
        with patch("flatsat_controller.simulation.socket.create_connection", return_value=bridge.client):
            result = run(bench, Mpu6050ABParameters(a, b, 2, 250, 0.2))
        frames = [json.loads(line) for line in (run_dir / "simulation-frames.jsonl").read_text().splitlines()]
        return result, frames, bridge

    def test_full_ab_capture_fragmented_tcp_and_artifacts(self):
        result, frames, bridge = self._run(other_host=True)
        self.assertEqual(result.outcome, "pass", result.reasons)
        self.assertEqual(bridge.samples, [11, 12])
        received = [frame for frame in frames if frame["direction"] == "rx"]
        self.assertGreaterEqual(len(received), 8)
        self.assertEqual(len(received), bridge.sent_frames)
        self.assertEqual(len([frame for frame in frames if frame["direction"] == "tx"]), 6)

    def test_mismatch_invalidates(self):
        result, _, _ = self._run(mismatch=True)
        self.assertEqual(result.outcome, "invalid")
        self.assertTrue(any("mismatch" in reason for reason in result.reasons))

    def test_drop_flags_and_status_counter_invalidate(self):
        result, _, _ = self._run(drop=True)
        self.assertEqual(result.outcome, "invalid")
        self.assertTrue(any("flags" in reason or "dropped" in reason for reason in result.reasons))

    def test_status_drop_counter_without_flags_invalidates(self):
        result, _, _ = self._run(counter_only=True)
        self.assertEqual(result.outcome, "invalid")
        self.assertTrue(any("dropped-transaction counter" in reason for reason in result.reasons))

    def test_transaction_id_gap_invalidates(self):
        result, _, _ = self._run(gap=True)
        self.assertEqual(result.outcome, "invalid")
        self.assertTrue(any("ID gap" in reason for reason in result.reasons))

    def test_final_status_catches_dropped_tail_record(self):
        result, _, _ = self._run(tail_drop=True)
        self.assertEqual(result.outcome, "invalid")
        self.assertTrue(any("tail loss" in reason for reason in result.reasons))

    def test_drains_valid_tail_record_queued_after_status(self):
        result, _, _ = self._run(tail_late=True)
        self.assertEqual(result.outcome, "pass", result.reasons)

    def test_consumes_valid_tail_received_during_final_status_poll(self):
        result, _, _ = self._run(tail_before_status=True)
        self.assertEqual(result.outcome, "pass", result.reasons)

    def test_flagged_prebaseline_queued_record_is_ignored(self):
        result, _, _ = self._run(old_flagged=True)
        self.assertEqual(result.outcome, "pass", result.reasons)

    def test_transaction_ids_and_counter_wrap_modulo_u32(self):
        result, _, _ = self._run(wrap_ids=True)
        self.assertEqual(result.outcome, "pass", result.reasons)

    def test_conversion_matches_independent_known_bytes_and_clips(self):
        a = Sample(1, (1.0, -1.0, 9.80665), (0.1, -0.2, 0.3), 30.0)
        p = Mpu6050ABParameters(a, a, 2, 250, 1)
        self.assertEqual(_expected_burst(a, p), bytes.fromhex("0687f9794000f75402effa2308cc"))
        clipped = Sample(2, (100.0, -100.0, 0.0), (100.0, -100.0, 0.0), -100.0)
        self.assertEqual(
            [int.from_bytes(_expected_burst(clipped, p)[i:i + 2], "big", signed=True) for i in range(0, 14, 2)],
            [32767, -32768, 0, -32768, 32767, -32768, 0],
        )

    def test_link_configuration_rejects_nonfinite_timeouts(self):
        for timeout in (float("nan"), float("inf"), 0):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                SimulationLinkConfig.from_mapping({"io_timeout_s": timeout})

    def test_failed_capture_open_closes_tcp_socket(self):
        client, peer = socket.socketpair()
        peer.settimeout(0.2)
        runtime = type("Runtime", (), {"run_dir": Path("/missing")})()
        link = SimulationLink(SimulationLinkConfig(), runtime)
        with (
            patch("flatsat_controller.simulation.socket.create_connection", return_value=client),
            patch.object(Path, "open", side_effect=OSError("capture unavailable")),
        ):
            with self.assertRaises(OSError):
                link.connect()
        self.assertEqual(peer.recv(1), b"")
        peer.close()

    def test_missing_second_read_times_out_invalid(self):
        result, _, _ = self._run(omit_second=True)
        self.assertEqual(result.outcome, "invalid")
        self.assertTrue(any("Timed out" in reason for reason in result.reasons))


if __name__ == "__main__":
    unittest.main()
