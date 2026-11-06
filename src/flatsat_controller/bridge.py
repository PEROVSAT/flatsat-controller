"""Controller capabilities for recording and observing the UTC bridge."""

from contextlib import contextmanager
from dataclasses import replace
from collections.abc import Iterator
import math
import threading
import time
from queue import Empty, Queue

from hitl_bridge.bridge import BridgeConfig, SyncObservation, open_serial, start_bridge
from hitl_bridge.verify_utc import UtcVerification, verify_utc
from flatsat_controller.runtime.context import RunContext
from flatsat_controller.simulation import SimulationLink, SimulationLinkConfig


class Bridge:
    def __init__(self, config: BridgeConfig, runtime: RunContext) -> None:
        self._config = config
        self._runtime = runtime
        self._log_path = runtime.run_dir / "bridge-utc.log"

    @contextmanager
    def start(self, name: str) -> Iterator[tuple[SimulationLink, "UtcObserver"]]:
        """Run one logical device UART through one ephemeral localhost bridge."""
        device = next((item for item in self._config.devices if item.name == name), None)
        if device is None:
            raise ValueError(f"no simulation link configured for {name!r}")
        config = replace(self._config, devices=(device,), listen_host="127.0.0.1", listen_port=0)
        stop = threading.Event()
        observations: Queue[SyncObservation] = Queue()

        def log(message: str) -> None:
            with self._log_path.open("a", encoding="utf-8") as stream:
                stream.write(message + "\n")

        self._log_path.touch(exist_ok=True)
        running = start_bridge(config, open_device=open_serial, stop=stop, log=log, on_sync=observations.put)
        try:
            link_config = SimulationLinkConfig(
                host="127.0.0.1", port=running.port,
                device_address=device.address,
            )
            observer = UtcObserver(observations, self._runtime, name)
            self._runtime.write_metadata("bridge", {
                "device": name, "listen_host": "127.0.0.1", "port": running.port,
                "uart": device.port, "baud": device.baud,
                "address": device.address,
            })
            yield SimulationLink(link_config, self._runtime), observer
        finally:
            stop.set()
            running.join()

    def verify_utc(
        self, name: str, *, timeout_s: float, tolerance_ns: int,
        require_correction: bool,
    ) -> UtcVerification:
        def log(message: str) -> None:
            with self._log_path.open("a", encoding="utf-8") as stream:
                stream.write(message + "\n")

        self._log_path.touch(exist_ok=True)
        try:
            result = verify_utc(
                self._config, name, timeout_s=timeout_s,
                tolerance_ns=tolerance_ns,
                require_correction=require_correction, log=log,
            )
        except Exception as exc:
            self._runtime.write_metadata("utc_evidence", {
                "error": f"{type(exc).__name__}: {exc}",
                "log": str(self._log_path),
            })
            raise
        self._runtime.write_metadata("utc_evidence", result)
        return result


class UtcObserver:
    def __init__(self, observations: Queue[SyncObservation], runtime: RunContext, name: str) -> None:
        self._observations = observations
        self._runtime = runtime
        self._name = name

    def verify_current(self, timeout_s: float, tolerance_ns: int) -> dict:
        """Require two accepted consecutive reports started during this check."""
        if not math.isfinite(timeout_s) or timeout_s <= 0 or timeout_s > threading.TIMEOUT_MAX:
            raise ValueError("UTC timeout must be positive, finite, and supported")
        if tolerance_ns <= 0:
            raise ValueError("UTC tolerance must be positive")
        started_ns = time.time_ns()
        deadline = time.monotonic() + timeout_s
        observations: list[SyncObservation] = []
        consecutive = 0
        last_id = None
        while time.monotonic() < deadline:
            try:
                item = self._observations.get(timeout=max(0.001, deadline - time.monotonic()))
            except Empty:
                break
            probe = item.probe
            if item.device != self._name or probe.t1_ns < started_ns:
                continue
            in_host_window = probe.t1_ns - tolerance_ns <= item.t3_ns <= item.t4_ns + tolerance_ns
            in_tolerance = abs(item.result.error_ns) <= tolerance_ns and in_host_window
            if not in_tolerance:
                consecutive = 0
                last_id = None
                continue
            observations.append(item)
            if consecutive == 0:
                consecutive += 1
            else:
                expected_id = 1 if last_id == 0xFFFFFFFF else last_id + 1
                if probe.id == expected_id:
                    consecutive += 1
                else:
                    observations = [item]
                    consecutive = 1
            last_id = probe.id
            if consecutive >= 2:
                break
        passed = consecutive >= 2
        evidence = {
            "device": self._name, "passed": passed,
            "reason": "UTC converged within tolerance on the running bridge" if passed else
                      "Timed out waiting for two current consecutive UTC reports within tolerance",
            "started_ns": started_ns, "tolerance_ns": tolerance_ns,
            "consecutive_in_tolerance": consecutive, "observations": tuple(observations),
        }
        self._runtime.write_metadata("utc_evidence", evidence)
        return evidence
