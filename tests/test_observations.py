from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hitl_bridge.link import encode_envelope

from flatsat_controller.simulation import SimulationLink, SimulationLinkConfig


class SimulationObservationTests(unittest.TestCase):
    def test_observes_unknown_frames_captures_and_closes_promptly(self):
        client, peer = socket.socketpair()
        server_closed = threading.Event()

        def serve():
            with peer:
                for frame in (b"unknown-kind-one", b"unknown-kind-two"):
                    peer.sendall(encode_envelope(frame))
                peer.settimeout(1)
                try:
                    while peer.recv(1024):
                        pass
                except (OSError, TimeoutError):
                    pass
            server_closed.set()

        server = threading.Thread(target=serve, daemon=True)
        server.start()
        with tempfile.TemporaryDirectory() as directory:
            link = SimulationLink(
                SimulationLinkConfig(io_timeout_s=0.05),
                SimpleNamespace(run_dir=Path(directory)),
            )
            frames: list[bytes] = []
            errors: list[Exception] = []
            delivered = threading.Event()

            def on_frame(frame: bytes) -> None:
                frames.append(frame)
                if len(frames) == 2:
                    delivered.set()

            with patch("flatsat_controller.simulation.socket.create_connection", return_value=client):
                with link.observe(on_frame, errors.append) as session:
                    self.assertTrue(delivered.wait(1))
                    self.assertEqual(session.received, [])
            self.assertTrue(server_closed.wait(1))
            self.assertEqual(frames, [b"unknown-kind-one", b"unknown-kind-two"])
            self.assertEqual(errors, [])
            records = [
                json.loads(line)
                for line in (Path(directory) / "simulation-observations.jsonl").read_text().splitlines()
            ]
            self.assertEqual([record["frame_hex"] for record in records], [
                b"unknown-kind-one".hex(), b"unknown-kind-two".hex()
            ])
        server.join(1)

    def test_reports_unexpected_disconnect(self):
        client, peer = socket.socketpair()
        peer.close()
        with tempfile.TemporaryDirectory() as directory:
            link = SimulationLink(
                SimulationLinkConfig(io_timeout_s=0.05),
                SimpleNamespace(run_dir=Path(directory)),
            )
            errors: list[Exception] = []
            reported = threading.Event()

            def on_error(error: Exception) -> None:
                errors.append(error)
                reported.set()

            with patch("flatsat_controller.simulation.socket.create_connection", return_value=client):
                with link.observe(lambda _frame: None, on_error) as session:
                    self.assertTrue(reported.wait(1))
                    self.assertTrue(session.closed)
            self.assertEqual(len(errors), 1)
            self.assertIn("closed the TCP link", str(errors[0]))

    def test_callback_failure_closes_connection_and_reports_error(self):
        client, peer = socket.socketpair()
        with tempfile.TemporaryDirectory() as directory:
            link = SimulationLink(
                SimulationLinkConfig(io_timeout_s=0.05),
                SimpleNamespace(run_dir=Path(directory)),
            )
            errors: list[Exception] = []
            reported = threading.Event()

            def on_error(error: Exception) -> None:
                errors.append(error)
                reported.set()

            peer.sendall(encode_envelope(b"observable"))
            with patch("flatsat_controller.simulation.socket.create_connection", return_value=client):
                with link.observe(lambda _frame: (_ for _ in ()).throw(ValueError("callback failed")), on_error) as session:
                    self.assertTrue(reported.wait(1))
                    self.assertTrue(session.closed)
                self.assertTrue(session.closed)
            peer.close()
            self.assertEqual(len(errors), 1)
            self.assertEqual(str(errors[0]), "callback failed")


if __name__ == "__main__":
    unittest.main()
