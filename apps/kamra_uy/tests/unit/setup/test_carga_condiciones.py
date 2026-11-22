"""HR-290: la pestaña «Seña y cancelación» de la planilla del hotel, leída y validada para la carga en Kamra: el cobro, la
retención, las horas de entrada y salida, la cancelación, el no-show, los niños y las mascotas. Cada error dice pestaña,
fila y columna; lo que la carga no sabe leer se avisa y no se toca."""

import pytest

from kamra_uy.setup import planilla
from kamra_uy.setup.tests import fabrica

SENA = "Seña y cancelación"


def _lee(tmp_path, cambios: dict | None = None, sena_=None):
	filas = [list(f) for f in (sena_ if sena_ is not None else fabrica.sena())]
	for i, valor in (cambios or {}).items():
		filas[i][1] = valor
	ruta = tmp_path / "planilla.xlsx"
	fabrica.escribe_xlsx(ruta, fabrica.pestanas(sena_=filas))
	return planilla.lee(ruta)


def _textos(notas):
	return [n.texto() for n in notas]


def test_the_tab_is_read_with_the_rest_of_the_planilla() -> None:
	assert SENA in planilla.PESTANAS


def test_the_invented_tab_reads_clean(tmp_path) -> None:
	p = _lee(tmp_path)
	assert p.errores == []
	c = p.condiciones
	assert c.pago_total is True
	assert c.retencion_minutos == 45
	assert (c.entrada, c.salida) == ("15:30:00", "10:15:00")
	assert c.cancelacion_gratis_dias == 9
	assert c.cargo_cancelacion == "Full Stay"  # 1 en una celda «0 %», como la del hotel
	assert c.cargo_no_show == "First Night"
	assert c.por_habitacion is True
	assert c.mascotas is False
	assert not [t for t in _textos(p.avisos) if SENA in t]


@pytest.mark.parametrize(
	("valor", "minutos"), [(120, 120), ("120 minutos", 120), ("90 min", 90), ("2 horas", 120), ("1 hora", 60)]
)
def test_the_hold_is_in_minutes_or_hours(tmp_path, valor, minutos) -> None:
	assert _lee(tmp_path, {2: valor}).condiciones.retencion_minutos == minutos


@pytest.mark.parametrize(("valor", "hora"), [("14:00", "14:00:00"), ("9:05", "09:05:00"), ("14 h", "14:00:00"),
                                             (0.4583333333333333, "11:00:00")])
def test_check_in_time_as_text_or_as_an_excel_time(tmp_path, valor, hora) -> None:
	assert _lee(tmp_path, {3: valor}).condiciones.entrada == hora


@pytest.mark.parametrize(("valor", "dias"), [(5, 5), ("5", 5), ("7 días", 7), (0, 0)])
def test_free_cancellation_days(tmp_path, valor, dias) -> None:
	assert _lee(tmp_path, {5: valor}).condiciones.cancelacion_gratis_dias == dias


@pytest.mark.parametrize(
	("valor", "cargo"),
	[(fabrica.Porcentaje(1), "Full Stay"), (fabrica.Porcentaje(1, propio=False), "Full Stay"), ("100%", "Full Stay"),
	 ("100 %", "Full Stay"), (100, "Full Stay"), ("Toda la estadía", "Full Stay"), ("El total", "Full Stay"),
	 ("La primera noche", "First Night"), ("Una noche", "First Night"), ("1 noche", "First Night"),
	 (fabrica.Porcentaje(0), "None"), ("0 %", "None"), (0, "None"), ("Nada", "None")],
)
def test_what_a_late_cancellation_or_a_no_show_loses(tmp_path, valor, cargo) -> None:
	c = _lee(tmp_path, {6: valor, 7: valor}).condiciones
	assert (c.cargo_cancelacion, c.cargo_no_show) == (cargo, cargo)


def test_a_bare_1_is_ambiguous_and_is_an_error(tmp_path) -> None:
	# el fundador (22-11): «No dice 1, dice 100%»; un 1 sin formato de porcentaje puede ser una noche o el 100 %
	p = _lee(tmp_path, {6: 1})
	assert _textos(p.errores) == [
		f"{SENA}, fila 10, columna B (Respuesta): «1»: ¿una noche o el 100 %? Escribe «100 %», «la primera noche» o "
		"«toda la estadía»."
	]


@pytest.mark.parametrize("valor", [fabrica.Porcentaje(0.5), "50 %", "2 noches"])
def test_a_charge_kamra_cannot_express_is_an_error(tmp_path, valor) -> None:
	p = _lee(tmp_path, {7: valor})
	(error,) = _textos(p.errores)
	assert error.startswith(f"{SENA}, fila 11, columna B (Respuesta): «")
	assert error.endswith("Kamra solo sabe cobrar la primera noche, el 100 % (toda la estadía) o nada.")


def test_an_unreadable_answer_is_an_error_with_its_row_and_column(tmp_path) -> None:
	p = _lee(tmp_path, {3: "por la tarde", 5: "unos días"})
	assert _textos(p.errores) == [
		f"{SENA}, fila 7, columna B (Respuesta): «por la tarde» no es una hora (escríbela como 14:00).",
		f"{SENA}, fila 9, columna B (Respuesta): «unos días» no es un número de días.",
	]


def test_a_deposit_that_is_not_the_whole_stay_is_an_error(tmp_path) -> None:
	p = _lee(tmp_path, {1: "30 %"})
	assert _textos(p.errores) == [
		f"{SENA}, fila 5, columna B (Respuesta): «30 %»: la carga solo sabe cobrar el total de la estadía al reservar "
		"(D-HR-38); para cobrar una seña, pregúntalo antes de cargar."
	]


def test_a_blank_answer_is_a_warning_and_is_left_alone(tmp_path) -> None:
	p = _lee(tmp_path, {5: None})
	assert p.errores == []
	assert p.condiciones.cancelacion_gratis_dias is None
	assert f"{SENA}, fila 9, columna B (Respuesta): en blanco: la carga no toca la cancelación gratis." in _textos(
		p.avisos
	)


def test_pets_are_read_from_the_other_rules_and_the_rest_is_a_warning(tmp_path) -> None:
	p = _lee(tmp_path, {9: "Sin mascotas. Salida tardía hasta las 13:00 con cargo."})
	assert p.condiciones.mascotas is False
	assert (
		f"{SENA}, fila 13, columna B (Respuesta): «Salida tardía hasta las 13:00 con cargo»: la carga no lo lee; "
		"ponlo en Kamra si hace falta." in _textos(p.avisos)
	)


def test_children_charged_per_person_are_not_understood_and_are_a_warning(tmp_path) -> None:
	p = _lee(tmp_path, {8: "Menores de 5 gratis"})
	assert p.condiciones.por_habitacion is None
	assert any("«Menores de 5 gratis»: la carga no lo lee" in t for t in _textos(p.avisos))


def test_columns_are_found_by_their_header_not_their_letter(tmp_path) -> None:
	filas = [["", *f] for f in fabrica.sena()]
	ruta = tmp_path / "planilla.xlsx"
	pest = fabrica.pestanas(sena_=filas)
	pest[SENA][2] = ["Orden", *fabrica.SENA_CABECERA]
	fabrica.escribe_xlsx(ruta, pest)
	p = planilla.lee(ruta)
	assert p.errores == []
	assert p.condiciones.cancelacion_gratis_dias == 9


def test_a_tab_without_its_header_is_an_error(tmp_path) -> None:
	ruta = tmp_path / "planilla.xlsx"
	fabrica.escribe_xlsx(ruta, fabrica.pestanas(extra={SENA: [["todavía habla de seña"]]}))
	p = planilla.lee(ruta)
	assert f"{SENA}: no encuentro la fila de cabecera con «Pregunta» y «Respuesta»." in _textos(p.errores)


def test_a_missing_question_is_a_warning(tmp_path) -> None:
	p = _lee(tmp_path, sena_=[f for f in fabrica.sena() if not f[0].startswith("Hora de salida")])
	assert p.errores == []
	assert p.condiciones.salida is None
	assert f"{SENA}: no encuentro la pregunta de la hora de salida: la carga no la toca." in _textos(p.avisos)
