from collections.abc import Mapping
from flatsat_controller.types import FlatsatError


def public_mock_selections(
    devices: Mapping[str, object],
    *,
    roles: Mapping[str, str],
) -> dict[str, str]:
    for role_name in roles:
        if role_name not in devices:
            raise FlatsatError(
                f"Role {role_name!r} is not a device in the catalog; "
                f"known devices: {sorted(devices)}"
            )

    selections: dict[str, str] = {}
    for name, spec in devices.items():
        if not isinstance(spec, dict):
            raise FlatsatError(f"Device {name!r} entry is not a mapping")
        modes_obj = spec.get("modes")
        if not isinstance(modes_obj, dict):
            raise FlatsatError(f"Device {name!r} has no modes mapping")
        modes = list(modes_obj.keys())

        if name in roles:
            mode = roles[name]
            if mode not in modes_obj:
                raise FlatsatError(
                    f"Device {name!r}: mode {mode!r} not in available modes {modes}"
                )
            selections[name] = mode
        elif "public-mock" in modes_obj:
            selections[name] = "public-mock"
        else:
            raise FlatsatError(
                f"Device {name!r} has no public-mock mode; available modes: {modes}"
            )
    return selections
