"""Carga por ruta los módulos de kamra_uy/health que no usan frappe (scrub.py y vigia.py), para que sus pruebas corran
igual dentro de `bench run-tests` y fuera, con `python3 -m unittest` desde esta carpeta. No empieza por `test_`: el
runner de Frappe no lo toma por un módulo de pruebas."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HEALTH = Path(__file__).resolve().parents[1]

try:
	from frappe.tests import UnitTestCase
except ImportError:  # fuera del bench
	from unittest import TestCase as UnitTestCase


def load(name: str):
	"""El módulo kamra_uy/health/<name>.py, cargado por ruta (sin importar el paquete, que importa frappe)."""
	key = f"_hr21_health_{name}"
	if key in sys.modules:
		return sys.modules[key]
	spec = importlib.util.spec_from_file_location(key, HEALTH / f"{name}.py")
	module = importlib.util.module_from_spec(spec)
	sys.modules[key] = module
	spec.loader.exec_module(module)
	return module
