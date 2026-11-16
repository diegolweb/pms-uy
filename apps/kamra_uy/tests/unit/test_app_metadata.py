"""Metadatos de la app kamra_uy, comprobados sin importar frappe (nivel rápido de gates)."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import kamra_uy

APP_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = APP_ROOT / "kamra_uy"


def _hooks() -> dict[str, object]:
    """Asignaciones de nivel superior de hooks.py con valor literal, sin ejecutar el módulo."""
    tree = ast.parse((PACKAGE / "hooks.py").read_text())
    values: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            values[node.targets[0].id] = ast.literal_eval(node.value)
    return values


def test_version() -> None:
    assert kamra_uy.__version__ == "0.1.0"


def test_requires_kamra() -> None:
    assert _hooks()["required_apps"] == ["kamra"]


def test_identity() -> None:
    hooks = _hooks()
    assert hooks["app_name"] == "kamra_uy"
    assert hooks["app_title"] == "Kamra UY"
    assert hooks["app_publisher"] == "Hotel Roosevelt"
    assert hooks["app_license"] == "agpl-3.0"


def test_does_not_declare_kamra_localization_yet() -> None:
    # La declaración de la localización llega con HR-23; antes de eso no hay nada que declarar.
    assert "kamra_localization" not in _hooks()


def test_targets_frappe_16() -> None:
    pyproject = tomllib.loads((APP_ROOT / "pyproject.toml").read_text())
    assert pyproject["tool"]["bench"]["frappe-dependencies"] == {"frappe": ">=16.0.0,<17.0.0"}
    assert pyproject["build-system"]["build-backend"] == "flit_core.buildapi"


def test_module_layout() -> None:
    assert (PACKAGE / "modules.txt").read_text().strip() == "Kamra UY"
    patches = (PACKAGE / "patches.txt").read_text()
    assert "[pre_model_sync]" in patches
    assert "[post_model_sync]" in patches
    assert (PACKAGE / "kamra_uy" / "__init__.py").is_file()


def test_pure_package_does_not_import_frappe() -> None:
    for source in (PACKAGE / "pure").rglob("*.py"):
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not any(n == "frappe" or n.startswith("frappe.") for n in names), f"{source} importa frappe"


def test_license_is_the_agpl_shipped_with_kamra() -> None:
    vendor_license = APP_ROOT.parents[1] / "vendor" / "kamra" / "license.txt"
    assert (APP_ROOT / "license.txt").read_text() == vendor_license.read_text()
