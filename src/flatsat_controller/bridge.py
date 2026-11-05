"""Narrow controller capability for recorded UTC bridge verification."""

from hitl_bridge.bridge import BridgeConfig
from hitl_bridge.verify_utc import UtcVerification, verify_utc
from flatsat_controller.runtime.context import RunContext


class Bridge:
    def __init__(self, config: BridgeConfig, runtime: RunContext) -> None:
        self._config = config
        self._runtime = runtime
        self._log_path = runtime.run_dir / "bridge-utc.log"

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
