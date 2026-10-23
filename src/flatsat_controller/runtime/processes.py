from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO


@dataclass
class ProcessHandle:
    pid: int
    argv: list[str]
    log_path: Path
    _proc: subprocess.Popen[bytes]
    _log_file: TextIO = field(repr=False)

    def poll(self) -> int | None:
        return self._proc.poll()

    @property
    def running(self) -> bool:
        return self._proc.poll() is None

    def close_log(self) -> None:
        if self._log_file.closed:
            return
        try:
            self._log_file.close()
        except OSError:
            pass


class ProcessGroup:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handles: list[ProcessHandle] = []

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        log_path: Path,
    ) -> int:
        handle = self._spawn(argv, cwd=cwd, env=env, log_path=log_path)
        try:
            return handle._proc.wait()
        finally:
            # An interrupt leaves the child tracked so close() can kill it.
            if handle.poll() is not None:
                self._drop(handle)

    def start(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        log_path: Path,
    ) -> ProcessHandle:
        return self._spawn(argv, cwd=cwd, env=env, log_path=log_path)

    def _spawn(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        log_path: Path,
    ) -> ProcessHandle:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("w", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        except Exception:
            log_file.close()
            raise
        handle = ProcessHandle(
            pid=proc.pid,
            argv=list(argv),
            log_path=log_path,
            _proc=proc,
            _log_file=log_file,
        )
        self._append(handle)
        return handle

    def close(self) -> list[str]:
        with self._lock:
            handles = list(self._handles)
            self._handles.clear()

        errors: list[str] = []
        for handle in handles:
            try:
                if handle.running:
                    handle._proc.terminate()
                    try:
                        handle._proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        handle._proc.kill()
                        handle._proc.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired) as exc:
                errors.append(f"pid {handle.pid}: {exc}")
            finally:
                handle.close_log()
        return errors

    def _append(self, handle: ProcessHandle) -> None:
        with self._lock:
            self._handles.append(handle)

    def _drop(self, handle: ProcessHandle) -> None:
        with self._lock:
            try:
                self._handles.remove(handle)
            except ValueError:
                pass
        handle.close_log()
