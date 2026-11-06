from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import yaml

from flatsat_controller.config import load_config
from flatsat_controller.firmware import (
    dbuild_argv,
    build_argv,
    flash_argv,
    flight_env,
    load_dbuild,
    write_dbuild,
)
from flatsat_controller.types import FlatsatError

class WriteDbuildTests(unittest.TestCase):
    def test_replaces_selections_keeps_devices(self) -> None:
        source = {
            "selections": {"IMU": "virtual"},
            "devices": {"IMU": {"modes": {"public-mock": {}}}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dbuild.yml"
            write_dbuild(source, {"IMU": "public-mock"}, path)
            doc = yaml.safe_load(path.read_text())
        self.assertEqual(doc["selections"], {"IMU": "public-mock"})
        self.assertEqual(doc["devices"], source["devices"])

    def test_preserves_extra_top_level_key(self) -> None:
        source = {
            "selections": {"IMU": "virtual"},
            "devices": {"IMU": {"modes": {"public-mock": {}}}},
            "notes": "keep me",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dbuild.yml"
            write_dbuild(source, {"IMU": "hardware"}, path)
            doc = yaml.safe_load(path.read_text())
        self.assertEqual(doc["notes"], "keep me")
        self.assertEqual(doc["selections"], {"IMU": "hardware"})


class ArgvAndEnvTests(unittest.TestCase):
    def test_plain_build_argv(self) -> None:
        argv = build_argv(Path("/work/.venv/bin/west"), board="rpi_pico2/rp2350a/m33", app_root=Path("/app"), build_dir=Path("/runs/build"))
        self.assertEqual(argv[1:4], ["build", "-b", "rpi_pico2/rp2350a/m33"])
        self.assertEqual(argv[-1], "/app")

    def test_dbuild_argv(self) -> None:
        west = Path("/flight/.venv/bin/west")
        config = Path("/runs/dbuild.yml")
        build_dir = Path("/runs/build")
        argv = dbuild_argv(west, board="rpi_pico2", config=config, build_dir=build_dir)
        self.assertEqual(
            argv,
            [
                str(west),
                "dbuild",
                "-b",
                "rpi_pico2",
                "--config",
                str(config),
                "-d",
                str(build_dir),
                "-p",
                "always",
            ],
        )
        self.assertTrue(Path(argv[5]).is_absolute())
        self.assertTrue(Path(argv[7]).is_absolute())

    def test_flash_argv(self) -> None:
        west = Path("/flight/.venv/bin/west")
        build_dir = Path("/runs/build")
        openocd = Path("/opt/openocd")
        argv = flash_argv(west, build_dir=build_dir, openocd=openocd)
        self.assertEqual(
            argv,
            [
                str(west),
                "flash",
                "--no-rebuild",
                "-d",
                str(build_dir),
                "-r",
                "openocd",
                "--openocd",
                str(openocd),
            ],
        )

    def test_flight_env(self) -> None:
        west = Path("/flight/.venv/bin/west")
        env = flight_env(west)
        self.assertNotIn("ZEPHYR_BASE", env)
        self.assertTrue(env["PATH"].startswith("/flight/.venv/bin" + os.pathsep))


class ConfigTests(unittest.TestCase):
    def _workspace(self, root: Path) -> None:
        (root / "flatsat-controller").mkdir(exist_ok=True)
        (root / ".west").mkdir(exist_ok=True)
        (root / ".west" / "config").touch()

    def test_logical_devices_and_flight_tool_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._workspace(root)
            (root / "app").mkdir()
            cfg_path = root / "bench.yml"
            cfg_path.write_text("flight: {app_root: ./app, west: ./flight-west}\ndevices:\n  imu: {probe_serial: ABC, uart: /dev/ttyUSB0, address: 1}\n  obc: {probe_serial: null}\n")
            cfg = load_config(cfg_path)
            self.assertEqual(cfg.require_target("imu").probe_serial, "ABC")
            self.assertEqual(cfg.require_uart("imu").address, 1)
            self.assertEqual(cfg.require_flight_app(), (root / "app").resolve())
            self.assertEqual(cfg.flight_west, (root / "flight-west").resolve())
            self.assertEqual(cfg.tools.west, (root / ".venv/bin/west").resolve())
            self.assertEqual(cfg.emulator_app, (root / "emulated-devices/mpu6050").resolve())
            with self.assertRaises(FlatsatError):
                cfg.require_target("obc")

    def test_duplicate_logical_device_keys_and_probe_serials_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._workspace(root)
            cfg_path = root / "bench.yml"
            cfg_path.write_text("devices:\n  imu: {address: 1}\n  imu: {address: 2}\n")
            with self.assertRaisesRegex(FlatsatError, "duplicate key"):
                load_config(cfg_path)
            cfg_path.write_text("devices:\n  imu: {probe_serial: ABC}\n  obc: {probe_serial: ABC}\n")
            with self.assertRaisesRegex(FlatsatError, "distinct probe_serial"):
                load_config(cfg_path)


if __name__ == "__main__":
    unittest.main()
