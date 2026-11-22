"""HR-273 (D-HR-50): la reserva en línea de la web casa cada tipo de Kamra con su ficha por su room_type_code
(web/src/booking/reglas.ts, `CODIGOS_TIPO`), y ese código lo pone la carga de la planilla (`plan.codigo`) a partir del
nombre del tipo. Esta guarda ata las dos puntas: el código que la carga daría a cada nombre de la planilla del fundador es
el que la web espera para su ficha. Si cambia `codigo` o la web, cae aquí y no en la página."""

import re
from pathlib import Path

import pytest

from kamra_uy.setup.plan import codigo

REGLAS = Path(__file__).resolve().parents[5] / "web" / "src" / "booking" / "reglas.ts"

# los nombres de la planilla del fundador (D-HR-39 y, desde HR-290, la del 22-11) y la ficha de la web de cada uno: la
# «Doble» es la de 2 camas individuales (la sección 2) y la «Doble Superior», la de 1 cama doble (la 3)
PLANILLA = {
	"Doble": "doble-camas-separadas",
	"Doble Superior": "doble-matrimonial",
	"Triple": "triple",
	"Apartamento": "apartamento",
}


def _codigos_web() -> dict[str, list[str]]:
	if not REGLAS.parent.parent.parent.is_dir():  # la fuente AGPL publicada va sin web/: aquí no hay otra punta que atar
		pytest.skip("sin web/ (kamra_uy fuera del monorepo)")
	texto = REGLAS.read_text(encoding="utf-8")
	bloque = re.search(r"export const CODIGOS_TIPO[^=]*=\s*\{(.*?)\n\};", texto, re.S)
	assert bloque, f"no encuentro CODIGOS_TIPO en {REGLAS}"
	return {
		m.group(1): re.findall(r"'([A-Z0-9]+)'", m.group(2))
		for m in re.finditer(r"^\s*'?([a-z-]+)'?:\s*\[([^\]]*)\]", bloque.group(1), re.M)
	}


def test_la_web_espera_el_codigo_que_la_carga_da_a_cada_nombre_de_la_planilla() -> None:
	web = _codigos_web()
	assert sorted(web) == sorted(PLANILLA.values())
	for nombre, clave in PLANILLA.items():
		assert codigo(nombre, set()) in web[clave], (nombre, codigo(nombre, set()), web[clave])


def test_los_codigos_de_las_dos_dobles() -> None:
	assert codigo("Doble", set()) == "DOBLE"
	assert codigo("Doble Superior", set()) == "DOBLESUPERIOR"
	# «Doble» nunca acaba con el código de la cama doble, ni la Superior con el de las 2 camas
	web = _codigos_web()
	assert "DOBLE" not in web["doble-matrimonial"] and "DOBLESUPERIOR" not in web["doble-camas-separadas"]
	# los nombres de D-HR-50, que nunca se cargaron, ya no son ninguna ficha
	assert all(codigo(n, set()) not in cs for n in ("Doble con camas separadas", "Doble Matrimonial") for cs in web.values())
