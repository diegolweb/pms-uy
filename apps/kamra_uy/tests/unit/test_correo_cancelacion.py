"""La cancelación del correo de confirmación (HR-291), sin frappe.

El correo dice la cancelación con los campos de la Property (`free_cancel_days`, `cancellation_fee`, `no_show_charge`),
sin cifras fijas. Con los de la planilla del 22-11 (5 días, «Full Stay» y «Full Stay», HR-290) dice lo mismo que la
página de condiciones de la web: la guarda lee web/content/{es,pt,en}/condiciones.md, que aquí (fuera del contenedor de
Kamra) sí está.
"""

import re
from pathlib import Path

import pytest

from kamra_uy.notifications import cancelacion, texts

RAIZ = Path(__file__).resolve().parents[4]
IDIOMAS = ("es", "pt", "en")


def _condiciones(idioma: str) -> tuple[str, list[str]]:
	"""(titular h2, sus textos) del bloque de cancelación de web/content/<idioma>/condiciones.md."""
	lineas = (RAIZ / "web" / "content" / idioma / "condiciones.md").read_text(encoding="utf-8").splitlines()
	titulo = texts.CANCELACION[idioma]["titulo"]
	(i,) = [n for n, linea in enumerate(lineas) if linea.strip() == f"- titular h2: {titulo}"]
	textos = []
	for linea in lineas[i + 1:]:
		if not linea.strip().startswith("- texto: "):
			break
		textos.append(linea.strip().removeprefix("- texto: "))
	return titulo, textos


@pytest.mark.parametrize("idioma", IDIOMAS)
def test_con_la_planilla_del_hotel_dice_lo_mismo_que_condiciones_md(idioma):
	# los dos primeros textos del bloque: el de cancelar y el de no llegar. Los otros dos (el medio de la devolución y
	# el correo y el WhatsApp del hotel para cancelar) son datos del hotel que kamra_uy (AGPL) no lleva: el contacto
	# del correo sale de la Property, en su apartado «Contacto»
	titulo, textos = _condiciones(idioma)
	assert len(textos) == 4
	assert cancelacion.parrafos(idioma, 5, "Full Stay", "Full Stay") == (titulo, textos[:2])


def test_los_dias_salen_de_la_property():
	assert cancelacion.parrafos("es", 9, "Full Stay", "Full Stay")[1][0].startswith(
		"Puedes cancelar sin costo hasta 9 días antes de la llegada")
	assert "hasta 1 día antes" in cancelacion.parrafos("es", 1, "Full Stay", "Full Stay")[1][0]
	assert "até 1 dia antes" in cancelacion.parrafos("pt", 1, "Full Stay", "Full Stay")[1][0]
	assert "up to 1 day before" in cancelacion.parrafos("en", 1, "Full Stay", "Full Stay")[1][0]
	assert "até 12 dias antes" in cancelacion.parrafos("pt", 12, "Full Stay", "Full Stay")[1][0]
	assert "up to 12 days before" in cancelacion.parrafos("en", 12, "Full Stay", "Full Stay")[1][0]


def test_la_primera_noche():
	_, (cancelar, no_llega) = cancelacion.parrafos("es", 7, "First Night", "First Night")
	assert cancelar == ("Puedes cancelar sin costo hasta 7 días antes de la llegada: te devolvemos el total. "
	                    "Si cancelas después, se cobra la primera noche.")
	assert no_llega == "Si no llegas y no nos avisas, se cobra la primera noche."
	_, (cancelar, no_llega) = cancelacion.parrafos("pt", 7, "First Night", "Full Stay")
	assert cancelar.endswith("Se cancelar depois, é cobrada a primeira noite.")
	assert no_llega == "Se você não chegar e não nos avisar, é cobrado 100% da estadia e não há devolução."
	_, (cancelar, no_llega) = cancelacion.parrafos("en", 7, "Full Stay", "First Night")
	assert cancelar.endswith("If you cancel later, 100% of the stay is charged and there is no refund.")
	assert no_llega == "If you do not arrive and do not let us know, the first night is charged."


@pytest.mark.parametrize("dias,cargo", [(0, "Full Stay"), (0, "First Night"), (5, "None"), (0, "None")])
def test_sin_cargo_antes_de_llegar_es_gratis_hasta_el_dia_de_la_llegada(dias, cargo):
	# Kamra cobra solo si quedan MENOS de free_cancel_days (kamra.api, inside_window): con 0, ni el mismo día de la
	# llegada; sin cargo («None»), nunca
	esperado = {
		"es": "Puedes cancelar sin costo hasta el día de la llegada: te devolvemos el total.",
		"pt": "Você pode cancelar sem custo até o dia da chegada: devolvemos o valor total.",
		"en": "You can cancel free of charge until the day of arrival: we refund the full amount.",
	}
	for idioma in IDIOMAS:
		assert cancelacion.parrafos(idioma, dias, cargo, "Full Stay")[1][0] == esperado[idioma]


def test_si_no_llega_y_no_se_cobra_lo_dice():
	assert cancelacion.parrafos("es", 5, "Full Stay", "None")[1][1] == "Si no llegas y no nos avisas, no se cobra nada."
	assert cancelacion.parrafos("pt", 5, "Full Stay", "None")[1][1] == "Se você não chegar e não nos avisar, nada é cobrado."
	assert cancelacion.parrafos("en", 5, "Full Stay", "None")[1][1] == (
		"If you do not arrive and do not let us know, nothing is charged.")


def test_vacios_como_kamra():
	# Kamra lee `free_cancel_days or 0` y `cancellation_fee or "None"` (kamra.api._cancellation_terms)
	assert cancelacion.parrafos("es", None, None, None) == cancelacion.parrafos("es", 0, "None", "None")
	assert cancelacion.parrafos("es", "5", "", "") == cancelacion.parrafos("es", 5, "None", "None")


def test_un_valor_que_kamra_no_tiene_se_rechaza():
	with pytest.raises(ValueError):
		cancelacion.parrafos("es", 5, "Half Stay", "Full Stay")
	with pytest.raises(ValueError):
		cancelacion.parrafos("es", 5, "Full Stay", "Half Stay")
	with pytest.raises(ValueError):
		cancelacion.parrafos("es", -1, "Full Stay", "Full Stay")


def test_con_tramos_no_dice_la_ventana_que_no_se_aplica():
	# con Property.cancellation_tiers, Kamra cobra por tramos y no por free_cancel_days: la ventana sería falsa
	assert cancelacion.parrafos("es", 5, "Full Stay", "First Night", tramos=True) == (
		"Cancelación", ["Si no llegas y no nos avisas, se cobra la primera noche."])


def test_una_estadia_no_reembolsable_lo_dice():
	# una noche en una temporada (o paquete) `non_refundable`: Kamra cobra el 100 % al cancelar, haya tramos o ventana
	esperado = {
		"es": "Esta reserva no es reembolsable: si cancelas, se cobra el 100% de la estadía y no hay devolución.",
		"pt": "Esta reserva não é reembolsável: se cancelar, é cobrado 100% da estadia e não há devolução.",
		"en": "This booking is non-refundable: if you cancel, 100% of the stay is charged and there is no refund.",
	}
	for idioma in IDIOMAS:
		titulo, (cancelar, no_llega) = cancelacion.parrafos(idioma, 5, "Full Stay", "Full Stay", tramos=True,
		                                                    no_reembolsable=True)
		assert cancelar == esperado[idioma]
		assert no_llega == _condiciones(idioma)[1][1]


def test_los_textos_no_llevan_cifras_fijas():
	# la única cifra es el 100 % de «Full Stay» (la estadía entera), que es lo que el valor quiere decir
	for idioma in IDIOMAS:
		for clave, valor in texts.CANCELACION[idioma].items():
			assert not re.search(r"\d", valor.replace("100%", "")), (idioma, clave)
	assert "{dias}" in texts.CANCELACION["es"]["gratis_hasta"]


def test_todos_los_idiomas_tienen_los_mismos_textos():
	claves = set(texts.CANCELACION["es"])
	for idioma in IDIOMAS:
		assert set(texts.CANCELACION[idioma]) == claves
		assert all(isinstance(v, str) and v.strip() for v in texts.CANCELACION[idioma].values())
	assert set(texts.CANCELACION_DE_LA_WEB) <= claves
