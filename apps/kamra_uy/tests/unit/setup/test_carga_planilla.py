"""La planilla del hotel leída y validada (HR-33): habitaciones, tipos y temporadas con precio, errores por fila."""

import datetime

import pytest

from kamra_uy.setup import planilla
from kamra_uy.setup.tests import fabrica

D = datetime.date


def _lee(tmp_path, **kw):
	ruta = tmp_path / "planilla.xlsx"
	fabrica.escribe_xlsx(ruta, fabrica.pestanas(**kw))
	return planilla.lee(ruta)


def _textos(notas):
	return [n.texto() for n in notas]


def test_the_invented_planilla_reads_clean(tmp_path) -> None:
	p = _lee(tmp_path)
	assert p.errores == []
	assert p.moneda == "USD"
	assert [h.numero for h in p.habitaciones] == ["801", "802", "803", "804", "805", "806"]
	assert {t.nombre: (t.adultos, t.ninos, len(t.habitaciones)) for t in p.tipos.values()} == {
		"Doble": (2, None, 2),
		"Triple": (3, None, 3),
		"Apartamento": (5, None, 1),
	}
	h = p.habitaciones[0]
	assert (h.tipo, h.camas, h.piso, h.notas, h.fila) == ("Doble", "2 camas individuales", "8", "Vista inventada A", 4)


def test_season_name_and_dates_are_carried_down_their_block(tmp_path) -> None:
	p = _lee(tmp_path)
	assert [(t.nombre, t.desde, t.hasta, t.tipo, t.precio, t.minimo) for t in p.temporadas[:3]] == [
		("Alta inventada", D(2030, 1, 1), D(2030, 1, 10), "Doble", 1001, 3),
		("Alta inventada", D(2030, 1, 1), D(2030, 1, 10), "Triple", 1502, 3),
		("Alta inventada", D(2030, 1, 1), D(2030, 1, 10), "Apartamento", 2003, 3),
	]
	assert len(p.temporadas) == 9


def test_a_blank_minimum_stay_is_one_night(tmp_path) -> None:
	p = _lee(tmp_path)
	baja = [t for t in p.temporadas if t.nombre == "Baja inventada" and t.tipo == "Doble"]
	assert baja[0].minimo == 1


def test_a_season_without_dates_stays_out_with_a_warning(tmp_path) -> None:
	p = _lee(tmp_path)
	assert "Julio inventado" not in {t.nombre for t in p.temporadas}
	assert p.sin_fechas == ["Julio inventado"]
	assert any("«Julio inventado» no tiene fechas" in t for t in _textos(p.avisos))


def test_the_base_price_of_a_type_is_its_lowest_season_price(tmp_path) -> None:
	p = _lee(tmp_path)
	assert {n: t.precio_minimo for n, t in p.tipos.items()} == {"Doble": 501, "Triple": 702, "Apartamento": 903}


def test_dates_typed_as_text_dd_mm_yyyy_are_read(tmp_path) -> None:
	tem = fabrica.temporadas()
	tem[0][1:3] = ["01/01/2030", "10/01/2030"]
	p = _lee(tmp_path, temporadas_=tem)
	assert p.errores == []
	assert (p.temporadas[0].desde, p.temporadas[0].hasta) == (D(2030, 1, 1), D(2030, 1, 10))


def test_an_impossible_date_is_an_error_with_sheet_row_and_column(tmp_path) -> None:
	tem = fabrica.temporadas()
	tem[0][2] = "31/02/2030"
	p = _lee(tmp_path, temporadas_=tem)
	assert _textos(p.errores) == [
		"Temporadas y precios, fila 4, columna C (Hasta): «31/02/2030» no es una fecha (escríbela como 15/12/2026)."
	]


def test_a_season_that_ends_before_it_starts_is_an_error(tmp_path) -> None:
	tem = fabrica.temporadas()
	tem[0][1:3] = [D(2030, 1, 10), D(2030, 1, 1)]
	p = _lee(tmp_path, temporadas_=tem)
	assert any("fila 4, columna C (Hasta): «Alta inventada» acaba antes de empezar" in t for t in _textos(p.errores))


def test_overlapping_seasons_are_an_error_naming_both(tmp_path) -> None:
	tem = fabrica.temporadas()
	tem[3][1] = D(2030, 1, 10)  # «Media» empieza el último día de «Alta»
	p = _lee(tmp_path, temporadas_=tem)
	assert _textos(p.errores) == [
		"Temporadas y precios, fila 7, columna B (Desde): «Media inventada» se pisa con «Alta inventada» (fila 4) el "
		"10-01-2030: Kamra no sabría qué precio cobrar."
	]


def test_a_gap_between_seasons_is_a_warning(tmp_path) -> None:
	tem = fabrica.temporadas()
	tem[3][1] = D(2030, 1, 13)
	p = _lee(tmp_path, temporadas_=tem)
	assert p.errores == []
	assert any("entre el 11-01-2030 y el 12-01-2030 no hay temporada" in t for t in _textos(p.avisos))


def test_season_and_room_types_must_match(tmp_path) -> None:
	tem = fabrica.temporadas()
	for fila in tem:
		if fila[3] == "Apartamento":
			fila[3] = "Familiar"
	p = _lee(tmp_path, temporadas_=tem)
	textos = _textos(p.errores)
	assert (
		"Temporadas y precios, fila 6, columna D (Tipo de habitación): «Familiar» no es un tipo de la pestaña "
		"Habitaciones (allí hay Apartamento, Doble y Triple): usa el mismo nombre en las dos." in textos
	)
	assert (
		"Habitaciones, fila 9, columna B (Tipo): «Apartamento» no tiene precio en «Temporadas y precios»." in textos
	)


def test_a_season_missing_one_type_is_an_error(tmp_path) -> None:
	tem = [f for f in fabrica.temporadas() if f[4] != 1202]  # sin el precio de Triple en «Media inventada»
	p = _lee(tmp_path, temporadas_=tem)
	assert (
		"Temporadas y precios, fila 7, columna D (Tipo de habitación): a «Media inventada» le falta el precio de Triple."
		in _textos(p.errores)
	)


def test_a_type_twice_in_one_season_is_an_error(tmp_path) -> None:
	tem = fabrica.temporadas()
	tem[1][3] = "Doble"
	p = _lee(tmp_path, temporadas_=tem)
	assert any(
		"fila 5, columna D (Tipo de habitación): «Doble» sale dos veces en «Alta inventada»" in t
		for t in _textos(p.errores)
	)


@pytest.mark.parametrize(
	("valor", "texto"),
	[
		(None, "falta el precio"),
		("caro", "«caro» no es un precio"),
		(0, "el precio tiene que ser mayor que 0"),
	],
)
def test_a_bad_price_is_an_error_on_its_cell(tmp_path, valor, texto) -> None:
	tem = fabrica.temporadas()
	tem[1][4] = valor
	p = _lee(tmp_path, temporadas_=tem)
	assert any(t.startswith("Temporadas y precios, fila 5, columna E (Precio por noche (US$)): " + texto)
		for t in _textos(p.errores)), _textos(p.errores)


def test_a_price_typed_as_text_with_comma_is_read(tmp_path) -> None:
	tem = fabrica.temporadas()
	tem[1][4] = "1502,50"
	p = _lee(tmp_path, temporadas_=tem)
	assert p.errores == []
	assert p.temporadas[1].precio == 1502.5


def test_a_bad_minimum_stay_is_an_error(tmp_path) -> None:
	tem = fabrica.temporadas()
	tem[1][5] = "dos"
	p = _lee(tmp_path, temporadas_=tem)
	assert any("fila 5, columna F (Estancia mínima (noches)): «dos» no es un número de noches" in t
		for t in _textos(p.errores))


def test_a_continuation_row_before_any_season_is_an_error(tmp_path) -> None:
	tem = fabrica.temporadas()
	tem[0][0] = None
	p = _lee(tmp_path, temporadas_=tem)
	assert "Temporadas y precios, fila 4, columna A (Temporada): falta el nombre de la temporada." in _textos(p.errores)


def test_currency_comes_from_the_price_header_and_unknown_is_an_error(tmp_path) -> None:
	ruta = tmp_path / "p.xlsx"
	pest = fabrica.pestanas()
	pest["Temporadas y precios"][2] = [*fabrica.TEM_CABECERA[:4], "Precio por noche", *fabrica.TEM_CABECERA[5:]]
	fabrica.escribe_xlsx(ruta, pest)
	p = planilla.lee(ruta)
	assert p.moneda is None
	assert (
		"Temporadas y precios, fila 3, columna E (Precio por noche): la cabecera no dice la moneda (p. ej. «(US$)»)."
		in _textos(p.errores)
	)


@pytest.mark.parametrize(("cabecera", "moneda"), [("Precio (US$)", "USD"), ("Precio en pesos ($U)", "UYU")])
def test_currency_headers(tmp_path, cabecera, moneda) -> None:
	ruta = tmp_path / "p.xlsx"
	pest = fabrica.pestanas()
	pest["Temporadas y precios"][2] = [*fabrica.TEM_CABECERA[:4], cabecera, *fabrica.TEM_CABECERA[5:]]
	fabrica.escribe_xlsx(ruta, pest)
	assert planilla.lee(ruta).moneda == moneda


def test_a_missing_header_is_an_error(tmp_path) -> None:
	ruta = tmp_path / "p.xlsx"
	pest = fabrica.pestanas()
	pest["Habitaciones"][2] = ["Número", "Tipo", "Camas"]
	fabrica.escribe_xlsx(ruta, pest)
	assert "Habitaciones: no encuentro la fila de cabecera con «Número», «Tipo» y «Adultos máx.»." in _textos(
		planilla.lee(ruta).errores
	)


def test_room_errors_by_row_and_column(tmp_path) -> None:
	hab = [list(f) for f in fabrica.HABITACIONES]
	hab[1][0] = "801"  # repetida
	hab[2][3] = "tres"
	hab[3][1] = None
	hab[4][4] = -1
	hab[5][0] = None
	p = _lee(tmp_path, habitaciones=hab)
	assert _textos(p.errores) == [
		"Habitaciones, fila 5, columna A (Número): la habitación 801 está repetida (también en la fila 4).",
		"Habitaciones, fila 6, columna D (Adultos máx.): «tres» no es un número de personas.",
		"Habitaciones, fila 7, columna B (Tipo): falta el tipo de la habitación.",
		"Habitaciones, fila 8, columna E (Niños máx.): «-1» no es un número de personas.",
		"Habitaciones, fila 9, columna A (Número): falta el número de la habitación.",
	]


def test_rooms_of_one_type_must_share_capacity(tmp_path) -> None:
	hab = [list(f) for f in fabrica.HABITACIONES]
	hab[4][3] = 4
	p = _lee(tmp_path, habitaciones=hab)
	assert _textos(p.errores) == [
		"Habitaciones, fila 8, columna D (Adultos máx.): la 805 es Triple para 4 adultos y la 803 (fila 6), para 3: "
		"en Kamra la capacidad es del tipo, no de la habitación."
	]


def test_blank_children_is_a_warning_per_type(tmp_path) -> None:
	p = _lee(tmp_path)
	assert sum("«Niños máx.» en blanco" in t for t in _textos(p.avisos)) == 3


def test_type_names_match_ignoring_case_and_keep_the_rooms_spelling(tmp_path) -> None:
	tem = fabrica.temporadas()
	tem[0][3] = "doble"
	p = _lee(tmp_path, temporadas_=tem)
	assert p.errores == []
	assert p.temporadas[0].tipo == "Doble"


def test_the_planilla_never_opens_the_personal_data_sheets(tmp_path) -> None:
	# las dos van ROTAS en la fábrica: si la carga las abriera, esto sería un error de lectura
	p = _lee(tmp_path)
	assert p.errores == []
	assert planilla.PESTANAS == ("Habitaciones", "Temporadas y precios")


def test_rooms_and_seasons_in_numbers(tmp_path) -> None:
	p = _lee(tmp_path)
	assert p.recuento() == {
		"habitaciones": 6,
		"por_tipo": {"Apartamento": 1, "Doble": 2, "Triple": 3},
		"temporadas": 3,
		"filas_de_precio": 9,
		"sin_fechas": ["Julio inventado"],
	}
