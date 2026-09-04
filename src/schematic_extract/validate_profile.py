from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .schema_rules import deep_get, get_required_paths


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str]
    warnings: list[str]


def validate_profile(profile: dict[str, Any]) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []

    component = profile.get("component")
    if not isinstance(component, dict):
        return ValidationResult(False, ["Missing component block"], warnings)

    category = component.get("category")
    if not category:
        errors.append("component.category is required")
        return ValidationResult(False, errors, warnings)

    for path in get_required_paths(category):
        value = deep_get(profile, path)
        if value is None:
            errors.append(f"Missing required section: {path}")

    if "component" in profile:
        for key in ["part_number", "manufacturer", "description", "category", "package"]:
            if not component.get(key):
                errors.append(f"component.{key} is required")

    schematic = profile.get("schematic", {})
    for supply in schematic.get("supply", []):
        if "unit" in supply and supply["unit"] != "V":
            errors.append("schematic.supply.unit must be 'V'")

    completeness = component.get("profile_completeness", [])
    if completeness and not isinstance(completeness, list):
        errors.append("component.profile_completeness must be a list")

    return ValidationResult(ok=(len(errors) == 0), errors=errors, warnings=warnings)
