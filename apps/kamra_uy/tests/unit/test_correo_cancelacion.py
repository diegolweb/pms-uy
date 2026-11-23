"""La cancelación del correo de confirmación (HR-291, y sus tramos: HR-301), sin frappe.

El correo dice la cancelación con los campos de la Property (`free_cancel_days`, `cancellation_fee`, `no_show_charge`),
sin cifras fijas. Con los de la planilla del 22-11 (5 días, «Full Stay» y «Full Stay», HR-290) dice lo mismo que la
página de condiciones de la web: la guarda lee web/content/{es,pt,en}/condiciones.md, que aquí (fuera del contenedor de
Kamra) sí está.
"""

import re
from itertools import pairwise
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


def _tramos(*pares):
	"""Filas de Property.cancellation_tiers como las lee Kamra: (days_before, charge_pct)."""
	return [{"days_before": d, "charge_pct": p} for d, p in pares]


def _kamra_tier_for(filas, days_before):
	"""kamra.api.cancellation_tier_for sin frappe (copia de su bucle): manda la banda de `days_before` más alto que no
	pase de los días que faltan; antes que todas, la última."""
	filas = sorted(filas, key=lambda t: int(t["days_before"] or 0), reverse=True)
	for t in filas:
		if int(days_before) >= int(t["days_before"] or 0):
			return float(t["charge_pct"] or 0)
	return float(filas[-1]["charge_pct"] or 0)


TRES = _tramos((7, 0), (3, 50), (0, 100))


def test_con_tramos_dice_cada_banda_en_vez_de_la_ventana():
	# con Property.cancellation_tiers, Kamra cobra por tramos y no por free_cancel_days (kamra.api._cancellation_terms)
	esperado = {
		"es": ["Lo que se cobra si cancelas depende de cuántos días antes de la llegada lo hagas:",
		       "7 días o más antes: no se cobra nada y te devolvemos el total.",
		       "De 3 a 6 días antes: se cobra el 50% de la estadía.",
		       "Menos de 3 días antes: se cobra el 100% de la estadía y no hay devolución.",
		       "Si no llegas y no nos avisas, se cobra la primera noche."],
		"pt": ["O valor cobrado se você cancelar depende de quantos dias antes da chegada você cancelar:",
		       "7 dias ou mais antes: nada é cobrado e devolvemos o valor total.",
		       "De 3 a 6 dias antes: é cobrado 50% da estadia.",
		       "Menos de 3 dias antes: é cobrado 100% da estadia e não há devolução.",
		       "Se você não chegar e não nos avisar, é cobrada a primeira noite."],
		"en": ["What you are charged if you cancel depends on how many days before arrival you do it:",
		       "7 days or more before: nothing is charged and we refund the full amount.",
		       "3 to 6 days before: 50% of the stay is charged.",
		       "Less than 3 days before: 100% of the stay is charged and there is no refund.",
		       "If you do not arrive and do not let us know, the first night is charged."],
	}
	for idioma in IDIOMAS:
		titulo, textos = cancelacion.parrafos(idioma, 5, "Full Stay", "First Night", tramos=TRES)
		assert titulo == texts.CANCELACION[idioma]["titulo"]
		assert textos == esperado[idioma]


def test_los_tramos_van_en_cualquier_orden_como_en_kamra():
	assert cancelacion.parrafos("es", 0, None, None, tramos=list(reversed(TRES))) == cancelacion.parrafos(
		"es", 0, None, None, tramos=TRES)


def test_la_banda_mas_baja_cubre_todo_lo_que_queda_hasta_la_llegada():
	# 14/0 y 5/50: Kamra cobra el 50 % también con menos de 5 días («antes que todas, la última»)
	_, textos = cancelacion.parrafos("es", 0, None, "None", tramos=_tramos((14, 0), (5, 50)))
	assert textos[1:] == ["14 días o más antes: no se cobra nada y te devolvemos el total.",
	                      "Menos de 14 días antes: se cobra el 50% de la estadía.",
	                      "Si no llegas y no nos avisas, no se cobra nada."]


def test_una_banda_de_un_solo_dia_y_el_dia_de_la_llegada():
	_, textos = cancelacion.parrafos("es", 0, None, "None", tramos=_tramos((7, 0), (6, 50), (1, 80), (0, 100)))
	assert textos[1:4] == ["7 días o más antes: no se cobra nada y te devolvemos el total.",
	                       "6 días antes: se cobra el 50% de la estadía.",
	                       "De 1 a 5 días antes: se cobra el 80% de la estadía."]
	assert textos[4] == "El día de la llegada: se cobra el 100% de la estadía y no hay devolución."
	assert cancelacion.parrafos("pt", 0, None, "None", tramos=_tramos((1, 0), (0, 100)))[1][1:3] == [
		"1 dia ou mais antes: nada é cobrado e devolvemos o valor total.",
		"No dia da chegada: é cobrado 100% da estadia e não há devolução."]
	assert cancelacion.parrafos("en", 0, None, "None", tramos=_tramos((2, 0), (1, 30), (0, 100)))[1][1:4] == [
		"2 days or more before: nothing is charged and we refund the full amount.",
		"1 day before: 30% of the stay is charged.",
		"On the day of arrival: 100% of the stay is charged and there is no refund."]


def test_con_una_sola_banda_se_cobra_lo_mismo_siempre():
	for filas, esperado in (
		(_tramos((3, 50)), "Si cancelas, en cualquier momento, se cobra el 50% de la estadía."),
		(_tramos((0, 0)), "Si cancelas, en cualquier momento, no se cobra nada y te devolvemos el total."),
		(_tramos((10, 100), (2, 100)),
		 "Si cancelas, en cualquier momento, se cobra el 100% de la estadía y no hay devolución."),
	):
		assert cancelacion.parrafos("es", 5, "Full Stay", "None", tramos=filas)[1] == [
			esperado, "Si no llegas y no nos avisas, no se cobra nada."]
	assert cancelacion.parrafos("pt", 0, None, "None", tramos=_tramos((3, 50)))[1][0] == (
		"Se você cancelar, a qualquer momento, é cobrado 50% da estadia.")
	assert cancelacion.parrafos("en", 0, None, "None", tramos=_tramos((3, 50)))[1][0] == (
		"If you cancel, at any time, 50% of the stay is charged.")


def test_dos_bandas_seguidas_con_el_mismo_cargo_se_dicen_juntas():
	_, textos = cancelacion.parrafos("es", 0, None, "None", tramos=_tramos((7, 0), (3, 50), (1, 50), (0, 100)))
	assert "De 1 a 6 días antes: se cobra el 50% de la estadía." in textos
	assert len(textos) == 5


def test_el_porcentaje_como_lo_cobra_kamra():
	# Kamra cobra percent_fee(res, pct): con pct <= 0 no cobra (inside_window = pct > 0); con decimales, el decimal
	_, textos = cancelacion.parrafos("es", 0, None, "None", tramos=_tramos((7, -5), (3, 12.5), (0, 150)))
	assert textos[1:4] == ["7 días o más antes: no se cobra nada y te devolvemos el total.",
	                       "De 3 a 6 días antes: se cobra el 12,5% de la estadía.",
	                       "Menos de 3 días antes: se cobra el 150% de la estadía y no hay devolución."]
	assert "12,5% da estadia" in cancelacion.parrafos("pt", 0, None, "None", tramos=_tramos((7, 0), (3, 12.5)))[1][2]
	assert "12.5% of the stay" in cancelacion.parrafos("en", 0, None, "None", tramos=_tramos((7, 0), (3, 12.5)))[1][2]


def test_vacios_y_negativos_como_kamra():
	# Kamra lee `days_before or 0` y `charge_pct or 0`; una banda bajo cero solo cuenta desde el día de la llegada
	assert cancelacion.parrafos("es", 0, None, None, tramos=_tramos((7, None), (None, 100))) == cancelacion.parrafos(
		"es", 0, None, None, tramos=_tramos((7, 0), (0, 100)))
	assert cancelacion.parrafos("es", 0, None, None, tramos=_tramos((3, 0), (-2, 100))) == cancelacion.parrafos(
		"es", 0, None, None, tramos=_tramos((3, 0), (0, 100)))


@pytest.mark.parametrize("pares", [
	((7, 0), (3, 50), (0, 100)), ((14, 0), (5, 50)), ((3, 50),), ((7, 0), (7, 100), (2, 30)), ((3, 0), (-2, 100)),
	((30, 0), (21, 10), (14, 25), (7, 50), (1, 75), (0, 100)), ((5, 100), (2, 0)), ((0, 40), (9, 0), (4, 40)),
	((6, -10), (2, 0), (0, 60)),
])
def test_las_bandas_dicen_lo_que_cobra_kamra_cada_dia(pares):
	# cada día que falte (de la llegada a más allá de la banda más alta) cae en un rango con el cargo de Kamra
	filas = _tramos(*pares)
	rangos = cancelacion.rangos(filas)
	assert rangos[0][0] == 0 and rangos[-1][1] is None
	for (_, hasta, _), (desde, _, _) in pairwise(rangos):
		assert desde == hasta + 1
	for dias in range(0, max(d for d, _ in pares) + 5):
		(pct,) = [p for desde, hasta, p in rangos if desde <= dias and (hasta is None or dias <= hasta)]
		assert pct == max(_kamra_tier_for(filas, dias), 0.0), dias  # con pct <= 0 Kamra no cobra


def test_una_estadia_no_reembolsable_lo_dice():
	# una noche en una temporada (o paquete) `non_refundable`: Kamra cobra el 100 % al cancelar, haya tramos o ventana
	esperado = {
		"es": "Esta reserva no es reembolsable: si cancelas, se cobra el 100% de la estadía y no hay devolución.",
		"pt": "Esta reserva não é reembolsável: se cancelar, é cobrado 100% da estadia e não há devolução.",
		"en": "This booking is non-refundable: if you cancel, 100% of the stay is charged and there is no refund.",
	}
	for idioma in IDIOMAS:
		titulo, (cancelar, no_llega) = cancelacion.parrafos(idioma, 5, "Full Stay", "Full Stay", tramos=TRES,
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
