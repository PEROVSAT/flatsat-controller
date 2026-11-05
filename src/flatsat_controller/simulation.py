from __future__ import annotations

import base64
import json
import math
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from collections.abc import Callable, Iterator
from typing import Any

from hitl_bridge.link import Deframer, encode_envelope

from flatsat_controller.types import FlatsatError


_capture_lock = threading.Lock()


@dataclass(frozen=True)
class SimulationLinkConfig:
    host: str = "127.0.0.1"
    port: int = 4242
    controller_address: int = 253
    device_address: int = 1
    connect_timeout_s: float = 3.0
    io_timeout_s: float = 0.5

    @classmethod
    def from_mapping(cls, raw: dict[str, object]) -> SimulationLinkConfig:
        allowed = {
            "host",
            "port",
            "controller_address",
            "device_address",
            "connect_timeout_s",
            "io_timeout_s",
        }
        if set(raw) - allowed:
            raise ValueError(f"unknown keys {sorted(set(raw) - allowed)}")
        host = raw.get("host", "127.0.0.1")
        if not isinstance(host, str) or not host:
            raise ValueError("host must be nonempty")
        values: dict[str, object] = {"host": host}
        for key, default, lo, hi in (
            ("port", 4242, 1, 65535),
            ("controller_address", 253, 0, 255),
            ("device_address", 1, 0, 255),
        ):
            value = raw.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise ValueError(f"{key} must be an integer in {lo}..{hi}")
            values[key] = value
        for key, default in (("connect_timeout_s", 3.0), ("io_timeout_s", 0.5)):
            value = raw.get(key, default)
            finite = isinstance(value, (int, float)) and not isinstance(value, bool)
            if finite:
                try:
                    finite = math.isfinite(value)
                except OverflowError:
                    finite = False
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not finite
                or value <= 0
            ):
                raise ValueError(f"{key} must be positive and finite")
            values[key] = float(value)
        if (
            values["controller_address"] == 254
            or values["device_address"] == 254
        ):
            raise ValueError("participant addresses cannot use the bridge address 254")
        if values["controller_address"] == values["device_address"]:
            raise ValueError("controller_address and device_address must differ")
        return cls(**values)


class SimulationLink:
    """Context-managed TCP simulation connection with byte-exact frame capture."""

    def __init__(self, config: SimulationLinkConfig, runtime: Any) -> None:
        self.config = config
        self.runtime = runtime

    def connect(
        self, *, retain_received: bool = True,
        capture_name: str = "simulation-frames.jsonl",
    ) -> SimulationSession:
        try:
            sock = socket.create_connection(
                (self.config.host, self.config.port), self.config.connect_timeout_s
            )
        except OSError as exc:
            raise FlatsatError(f"Could not connect to simulation bridge: {exc}") from exc
        try:
            sock.settimeout(self.config.io_timeout_s)
            return SimulationSession(
                sock, self.config, self.runtime, retain_received=retain_received,
                capture_name=capture_name,
            )
        except BaseException:
            sock.close()
            raise

    @contextmanager
    def observe(
        self,
        on_frame: Callable[[bytes], None],
        on_error: Callable[[Exception], None],
    ) -> Iterator[SimulationSession]:
        """Continuously deliver every inbound frame on a dedicated bridge link.

        Callbacks run synchronously on the observer thread and should return promptly.
        """
        session = self.connect(
            retain_received=False, capture_name="simulation-observations.jsonl"
        )
        stopping = threading.Event()

        def report(exc: Exception) -> None:
            stopping.set()
            session.close()
            try:
                on_error(exc)
            except Exception:
                pass

        def drain() -> None:
            while not stopping.is_set():
                try:
                    frame = session.receive(self.config.io_timeout_s)
                except TimeoutError:
                    continue
                except Exception as exc:
                    if not stopping.is_set():
                        report(exc)
                    return
                try:
                    on_frame(frame)
                except Exception as exc:
                    if not stopping.is_set():
                        report(exc)
                    return

        worker = threading.Thread(target=drain, name="simulation-observer", daemon=True)
        try:
            worker.start()
        except BaseException:
            stopping.set()
            session.close()
            raise
        try:
            yield session
        finally:
            stopping.set()
            try:
                session.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            worker.join()
            session.close()


class SimulationSession:
    def __init__(
        self, sock: socket.socket, config: SimulationLinkConfig, runtime: Any,
        *, retain_received: bool = True,
        capture_name: str = "simulation-frames.jsonl",
    ) -> None:
        self.sock = sock
        self.config = config
        self.runtime = runtime
        self.deframer = Deframer()
        self.pending: list[bytes] = []
        self.received: list[bytes] = []
        self.retain_received = retain_received
        self.closed = False
        self.capture = (runtime.run_dir / capture_name).open("a", encoding="utf-8")

    def __enter__(self) -> SimulationSession:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        finally:
            with _capture_lock:
                self.capture.close()

    @property
    def pending_envelope_bytes(self) -> int:
        return len(getattr(self.deframer, "_buffer", ()))

    def send_frame(self, frame: bytes) -> None:
        try:
            self.sock.sendall(encode_envelope(frame))
        except OSError as exc:
            raise FlatsatError(f"Simulation bridge write failed: {exc}") from exc
        self._capture("tx", frame)

    def receive(self, timeout_s: float) -> bytes:
        deadline = time.monotonic() + timeout_s
        while True:
            if self.pending:
                return self.pending.pop(0)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for simulation frame")
            self.sock.settimeout(remaining)
            try:
                data = self.sock.recv(4096)
            except TimeoutError as exc:
                raise TimeoutError("Timed out waiting for simulation frame") from exc
            except OSError as exc:
                raise FlatsatError(f"Simulation bridge read failed: {exc}") from exc
            if not data:
                raise FlatsatError("Simulation bridge closed the TCP link")
            frames = self.deframer.push(data)
            for frame in frames:
                self._capture("rx", frame)
            self.pending.extend(frames)
            if self.retain_received:
                self.received.extend(frames)

    def _capture(self, direction: str, frame: bytes) -> None:
        record = {
            "monotonic_ns": time.monotonic_ns(),
            "direction": direction,
            "frame_hex": frame.hex(),
            "frame_base64": base64.b64encode(frame).decode("ascii"),
        }
        with _capture_lock:
            self.capture.write(json.dumps(record) + "\n")
            self.capture.flush()
