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
    public_mock_selections,
    write_dbuild,
)
from flatsat_controller.types import FlatsatError

REAL_DEVICES = {
    "IMU": {
        "modes": {
            "public-mock": {},
            "library-mock": {},
            "simulation": {},
            "hardware": {},
        }
    },
    "AMU": {
        "modes": {
            "public-mock": {},
            "library-mock": {},
            "hardware": {},
        }
    },
    "FLASH": {"modes": {"virtual": {}, "pico2": {}}},
    "CONSOLE": {"modes": {"no-console": {}, "usb": {}, "logs": {}}},
}


class PublicMockSelectionsTests(unittest.TestCase):
    def test_real_catalog_defaults_and_roles(self) -> None:
        sel = public_mock_selections(
            REAL_DEVICES,
            roles={"FLASH": "pico2", "CONSOLE": "logs"},
        )
        self.assertEqual(
            sel,
            {
                "IMU": "public-mock",
                "AMU": "public-mock",
                "FLASH": "pico2",
                "CONSOLE": "logs",
            },
        )

    def test_no_public_mock_and_no_role_raises(self) -> None:
        devices = {"SENSOR": {"modes": {"hardware": {}}}}
        with self.assertRaises(FlatsatError) as ctx:
            public_mock_selections(devices, roles={})
        self.assertIn("SENSOR", str(ctx.exception))

    def test_invalid_role_mode_raises(self) -> None:
        with self.assertRaises(FlatsatError) as ctx:
            public_mock_selections(REAL_DEVICES, roles={"FLASH": "public-mock"})
        self.assertIn("FLASH", str(ctx.exception))

    def test_unknown_device_role_raises(self) -> None:
        with self.assertRaises(FlatsatError) as ctx:
            public_mock_selections(REAL_DEVICES, roles={"TYPO": "public-mock"})
        self.assertIn("TYPO", str(ctx.exception))

    def test_explicit_role_overrides_public_mock(self) -> None:
        devices = {
            "IMU": REAL_DEVICES["IMU"],
            "AMU": REAL_DEVICES["AMU"],
        }
        sel = public_mock_selections(devices, roles={"IMU": "hardware"})
        self.assertEqual(sel["IMU"], "hardware")
        self.assertEqual(sel["AMU"], "public-mock")


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
    def test_relative_app_root_against_config_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "app"
            app.mkdir()
            cfg_path = root / "bench.yml"
            cfg_path.write_text("app_root: ./app\nboard: rpi_pico2\n")
            cfg = load_config(cfg_path)
            self.assertEqual(cfg.builds["default"].app_root, app.resolve())

    def test_unknown_keys_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "bench.yml"
            cfg_path.write_text("app_root: .\nboard: x\nextra: 1\n")
            with self.assertRaises(FlatsatError):
                load_config(cfg_path)

    def test_named_build_kind_workspace_and_default_board(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app").mkdir()
            cfg_path = root / "bench.yml"
            cfg_path.write_text("builds:\n  plain:\n    app_root: ./app\n    kind: build\n    west: ./west\n")
            request = load_config(cfg_path).builds["plain"]
            self.assertEqual(request.kind, "build")
            self.assertEqual(request.west, (root / "west").resolve())
            self.assertEqual(request.board, "rpi_pico2/rp2350a/m33")

    def test_named_builds_cannot_inherit_another_builds_west(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app").mkdir()
            cfg_path = root / "bench.yml"
            cfg_path.write_text(
                "builds:\n  workspace_a:\n    app_root: ./app\n    west: ./west-a\n"
                "  workspace_b:\n    app_root: ./app\n"
            )
            with self.assertRaisesRegex(FlatsatError, "Each named build needs west"):
                load_config(cfg_path)

    def test_named_builds_with_each_west_need_no_global_west(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app").mkdir()
            cfg_path = root / "bench.yml"
            cfg_path.write_text(
                "builds:\n  a:\n    app_root: ./app\n    west: ./west-a\n"
                "  b:\n    app_root: ./app\n    west: ./west-b\n"
            )
            loaded = load_config(cfg_path)
            self.assertIsNone(loaded.installation.tools.west)
            self.assertEqual(loaded.builds["b"].west, (root / "west-b").resolve())

    def test_multiple_targets_require_unique_probe_serials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_path = root / "bench.yml"
            cfg_path.write_text("targets:\n  a:\n    probe_serial: ABC\n  b:\n    probe_serial: ABC\n")
            with self.assertRaisesRegex(FlatsatError, "distinct explicit probe_serial"):
                load_config(cfg_path)

    def test_explicit_non_pico2_board_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app").mkdir()
            cfg_path = root / "bench.yml"
            cfg_path.write_text("builds:\n  app:\n    app_root: ./app\n    board: native_sim\n")
            with self.assertRaisesRegex(FlatsatError, "only Pico 2"):
                load_config(cfg_path)

    def test_unhashable_kind_fails_as_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app").mkdir()
            cfg_path = root / "bench.yml"
            cfg_path.write_text("builds:\n  app:\n    app_root: ./app\n    kind: [build]\n")
            with self.assertRaisesRegex(FlatsatError, "kind must be"):
                load_config(cfg_path)


class RealDbuildIntegration(unittest.TestCase):
    def test_example_config_selections(self) -> None:
        repo = Path(__file__).resolve().parent.parent
        cfg_path = repo / "configs" / "pico2_public_mock.yml"
        self.assertTrue(cfg_path.is_file(), "example config missing")
        cfg = load_config(cfg_path)
        dbuild_path = cfg.builds["default"].app_root / "dbuild.yml"
        self.assertTrue(dbuild_path.is_file(), "perovsat-app dbuild.yml not present")
        devices = load_dbuild(cfg.builds["default"].app_root)["devices"]
        sel = public_mock_selections(
            devices,
            roles={"FLASH": "pico2", "CONSOLE": "logs"},
        )
        self.assertEqual(
            sel,
            {
                "IMU": "public-mock",
                "AMU": "public-mock",
                "FLASH": "pico2",
                "CONSOLE": "logs",
            },
        )
        self.assertEqual(
            cfg.installation.tools.openocd,
            (Path.home() / ".local" / "bin" / "openocd").resolve(),
        )


if __name__ == "__main__":
    unittest.main()
