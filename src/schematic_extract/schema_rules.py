from __future__ import annotations

REQUIRED_BY_CATEGORY = {
    "sensor": [
        "component",
        "schematic.supply",
        "schematic.pinout",
        "schematic.decoupling",
        "schematic.design_checks",
        "software.interface",
        "software.registers",
        "software.init_sequence",
    ],
    "power": [
        "component",
        "schematic.supply",
        "schematic.pinout",
        "schematic.decoupling",
        "schematic.design_checks",
        "schematic.required_external",
    ],
    "mcu": [
        "component",
        "schematic.supply",
        "schematic.pinout",
        "schematic.decoupling",
        "schematic.design_checks",
    ],
    "mux": [
        "component",
        "schematic.supply",
        "schematic.pinout",
        "schematic.design_checks",
        "software.interface",
        "software.init_sequence",
    ],
    "interface": [
        "component",
        "schematic.supply",
        "schematic.pinout",
        "software.interface",
    ],
    "passive": [
        "component",
        "schematic.supply",
    ],
}


def get_required_paths(category: str) -> list[str]:
    return REQUIRED_BY_CATEGORY.get(category, [])


def deep_get(data: dict, path: str):
    current = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current