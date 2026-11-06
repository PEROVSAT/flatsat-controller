from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import os
import re
import stat

import yaml

from flatsat_controller.firmware import FirmwareTools, Target, validate_probe_serial
from flatsat_controller.firmware.types import PICO2_BOARD
from flatsat_controller.types import FlatsatError
from hitl_bridge.bridge import BridgeConfig, DeviceConfig


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    result: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                "mapping keys must be strings", key_node.start_mark,
            )
        if key in result:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                f"duplicate key {key!r}", key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


@dataclass(frozen=True)
class Device:
    """One logical bench device, with its optional probe and UART identity."""

    name: str
    probe_serial: str | None = None
    uart: str | None = None
    address: int | None = None
    baud: int = 115200


@dataclass(frozen=True)
class BenchConfig:
    workspace: Path
    devices: Mapping[str, Device]
    tools: FirmwareTools
    flight_app: Path | None = None
    west: Path | None = None

    def require_target(self, name: str) -> Target:
        device = self._require_device(name)
        if not device.probe_serial:
            raise FlatsatError(f"Device {name!r} needs an explicit devices.{name}.probe_serial for flashing")
        serial = device.probe_serial.lower()
        if (serial in {"serial", "your_serial", "replace_me", "todo", "unknown"}
                or "replace" in serial or serial.startswith("set-")
                or serial.startswith("your-")
                or serial.startswith("<") or serial.endswith("_here")):
            raise FlatsatError(f"Device {name!r} has a placeholder probe_serial; enter the probe's actual serial")
        return Target(name, PICO2_BOARD, device.probe_serial)

    def require_uart(self, name: str) -> DeviceConfig:
        device = self._require_device(name)
        if not device.uart:
            raise FlatsatError(f"Device {name!r} needs devices.{name}.uart for UART access")
        if device.address is None:
            raise FlatsatError(f"Device {name!r} needs devices.{name}.address for simulation access")
        return DeviceConfig(name, device.address, device.uart, device.baud)

    @property
    def emulator_app(self) -> Path:
        return self.workspace / "emulated-devices" / "mpu6050"

    def require_flight_app(self) -> Path:
        if self.flight_app is None:
            raise FlatsatError("Set flight.app_root in workspace bench.yml before running this experiment")
        if not self.flight_app.is_dir():
            raise FlatsatError(f"flight.app_root is not a directory: {self.flight_app}")
        return self.flight_app

    @property
    def flight_west(self) -> Path | None:
        if self.west is not None:
            return self.west
        if self.flight_app is None:
            return None
        return self.flight_app.parent / ".venv" / "bin" / "west"

    @property
    def bridge_config(self) -> BridgeConfig:
        """Build the public bridge projection from the one devices mapping."""
        uart_devices = tuple(
            self.require_uart(name) for name, item in self.devices.items() if item.uart
        )
        return BridgeConfig(
            devices=uart_devices,
            listen_host="127.0.0.1",
            listen_port=0,
        )

    def _require_device(self, name: str) -> Device:
        try:
            return self.devices[name]
        except KeyError as exc:
            raise FlatsatError(f"No logical device named {name!r} in bench.yml") from exc


def find_workspace(start: Path | None = None) -> Path:
    """Locate the checkout root from cwd or this installed controller package."""
    starts = [start.resolve()] if start is not None else [Path.cwd(), Path(__file__).resolve()]
    seen: set[Path] = set()
    for item in starts:
        for parent in (item, *item.parents):
            if parent in seen:
                continue
            seen.add(parent)
            if (parent / "flatsat-controller").is_dir() and _is_workspace(parent):
                return parent
            # When running from inside the controller checkout itself.
            if parent.name == "flatsat-controller" and _is_workspace(parent.parent):
                return parent.parent
    raise FlatsatError("Could not locate FlatSat workspace (expected flatsat-controller and west.yml)")


def load_config(path: Path) -> BenchConfig:
    try:
        config_path = path.expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise FlatsatError(f"Invalid config path {path}: {exc}") from exc
    if not config_path.is_file():
        raise FlatsatError(f"Config file not found: {config_path}")
    try:
        raw = yaml.load(config_path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise FlatsatError(f"Could not read config {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise FlatsatError(f"Config must be a YAML mapping: {config_path}")
    unknown = set(raw) - {"tools", "flight", "devices"}
    if unknown:
        raise FlatsatError(f"Unknown config keys {sorted(map(str, unknown))} in {config_path}")

    workspace = find_workspace(config_path.parent)
    tools_data = raw.get("tools", {})
    if tools_data is None:
        tools_data = {}
    if not isinstance(tools_data, dict) or set(tools_data) - {"openocd"}:
        raise FlatsatError("tools must be a mapping with optional openocd")
    openocd_value = tools_data.get("openocd")
    if openocd_value is None:
        openocd_value = "~/.local/bin/openocd"
    openocd = _resolve_path(openocd_value, workspace)
    flight_data = raw.get("flight", {})
    if flight_data is None:
        flight_data = {}
    if not isinstance(flight_data, dict) or set(flight_data) - {"app_root", "west"}:
        raise FlatsatError("flight must be a mapping with app_root and optional west")
    flight_app = _resolve_path(flight_data["app_root"], workspace) if flight_data.get("app_root") is not None else None
    west = _resolve_path(flight_data["west"], workspace) if flight_data.get("west") is not None else None
    firmware_tools = FirmwareTools(workspace / ".venv" / "bin" / "west", openocd)

    device_data = raw.get("devices", {})
    if not isinstance(device_data, dict):
        raise FlatsatError("devices must be a mapping keyed by logical device name")
    devices: dict[str, Device] = {}
    addresses: set[int] = set()
    uarts: set[str] = set()
    for name, spec in device_data.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
            raise FlatsatError(f"Invalid logical device name: {name!r}")
        if not isinstance(spec, dict) or set(spec) - {"probe_serial", "uart", "address", "baud"}:
            raise FlatsatError(f"Device {name}: expected probe_serial, uart, address, baud")
        serial = spec.get("probe_serial")
        uart = spec.get("uart")
        address = spec.get("address")
        baud = spec.get("baud", 115200)
        if serial is not None and (not isinstance(serial, str) or not serial.strip()):
            raise FlatsatError(f"Device {name}: probe_serial must be a non-empty string or null")
        try:
            validate_probe_serial(serial)
        except FlatsatError as exc:
            raise FlatsatError(f"Device {name}: {exc}") from exc
        if uart is not None and (not isinstance(uart, str) or not uart.strip()):
            raise FlatsatError(f"Device {name}: uart must be a non-empty device path or null")
        if address is not None and (type(address) is not int or not 1 <= address <= 255):
            raise FlatsatError(f"Device {name}: address must be an integer from 1 to 255")
        if address in {253, 254}:
            raise FlatsatError(f"Device {name}: address {address} is reserved by the simulation protocol")
        if type(baud) is not int or baud <= 0:
            raise FlatsatError(f"Device {name}: baud must be a positive integer")
        if uart is not None and address is None:
            raise FlatsatError(f"Device {name}: address is required when uart is configured")
        if address is not None and address in addresses:
            raise FlatsatError(f"Device {name}: duplicate simulation address {address}")
        if address is not None:
            addresses.add(address)
        if uart is not None:
            uart_path = Path(uart).expanduser()
            if not uart_path.is_absolute():
                uart = str((workspace / uart_path).absolute())
            else:
                uart = str(uart_path.absolute())
        if uart is not None:
            uart_identity = str(Path(uart).resolve())
            if uart_identity in uarts:
                raise FlatsatError(f"Device {name}: UART path {uart!r} is already assigned")
            uarts.add(uart_identity)
        devices[name] = Device(name, serial, uart, address, baud)
    serials = [device.probe_serial for device in devices.values() if device.probe_serial]
    if len(serials) != len(set(serials)):
        raise FlatsatError("Each configured device must have a distinct probe_serial")
    return BenchConfig(workspace, devices, firmware_tools, flight_app, west)


def _resolve_path(value: object, base: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise FlatsatError("Paths must be non-empty strings")
    try:
        path = Path(value).expanduser()
        return (base / path).resolve() if not path.is_absolute() else path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise FlatsatError(f"Invalid path {value!r}: {exc}") from exc


def check_config(config: BenchConfig) -> list[str]:
    """Inspect configured paths, UART metadata and USB serials without opening ports or running builds/flashes."""
    errors: list[str] = []
    for label, path in (("west", config.tools.west), ("openocd", config.tools.openocd)):
        if path is None or not path.is_file() or not os.access(path, os.X_OK):
            errors.append(f"{label} is missing or not executable: {path}")
    if not config.emulator_app.is_dir():
        errors.append(f"Emulator app is missing: {config.emulator_app}")
    if config.flight_app is not None and not config.flight_app.is_dir():
        errors.append(f"Flight app is missing: {config.flight_app}")
    serials_to_check = {d.probe_serial for d in config.devices.values() if d.probe_serial}
    attached: set[str] = set()
    inspection_error: str | None = None
    if serials_to_check:
        attached, inspection_error = _usb_serials()
        if inspection_error:
            errors.append(f"Could not inspect USB debug probes: {inspection_error}")
    for device in config.devices.values():
        if device.uart:
            path = Path(device.uart)
            try:
                mode = path.stat().st_mode
            except OSError:
                mode = 0
            if not stat.S_ISCHR(mode) or not os.access(path, os.R_OK | os.W_OK):
                errors.append(f"UART is unavailable or not a readable/writable character device: {device.name} ({device.uart})")
        if device.probe_serial:
            if not inspection_error and device.probe_serial not in attached:
                errors.append(f"Debug probe {device.probe_serial!r} for {device.name} was not found in USB enumeration")
    if config.flight_west is not None:
        flight_west = config.flight_west
        if not flight_west.is_file() or not os.access(flight_west, os.X_OK):
            errors.append(f"flight west is missing or not executable: {flight_west}")
    if config.flight_app is not None:
        dbuild = config.flight_app / "dbuild.yml"
        if not dbuild.is_file() or not os.access(dbuild, os.R_OK):
            errors.append(f"Flight dbuild.yml is missing or unreadable: {dbuild}")
    return errors


def _is_workspace(path: Path) -> bool:
    return (path / ".west" / "config").is_file() or (
        (path / "west.yml").is_file()
        and (path / "emulated-devices").is_dir()
        and (path / "zephyr").is_dir()
    )


def _usb_serials() -> tuple[set[str], str | None]:
    """Read USB serials using platform enumeration without opening devices."""
    import platform
    import plistlib
    import subprocess

    if platform.system() == "Darwin":
        try:
            result = subprocess.run(
                ["/usr/sbin/ioreg", "-a", "-r", "-c", "IOUSBHostDevice"],
                check=True, capture_output=True, timeout=10,
            )
            payload = plistlib.loads(result.stdout) if result.stdout else []
            serials: set[str] = set()
            def visit(value: object) -> None:
                if isinstance(value, dict):
                    serial = value.get("USB Serial Number") or value.get("kUSBSerialNumberString")
                    if isinstance(serial, str) and serial:
                        serials.add(serial)
                    for child in value.values():
                        visit(child)
                elif isinstance(value, list):
                    for child in value:
                        visit(child)
            visit(payload)
            return serials, None
        except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, ValueError) as exc:
            return set(), str(exc)
    if platform.system() == "Linux":
        try:
            serials = set()
            for path in Path("/sys/bus/usb/devices").glob("*/serial"):
                try:
                    serial = path.read_text(encoding="utf-8").strip()
                except OSError:
                    continue
                if serial:
                    serials.add(serial)
            return serials, None
        except OSError as exc:
            return set(), str(exc)
    return set(), f"USB serial enumeration is unsupported on {platform.system()}"
