"""El lector .xlsx de la carga (HR-33): biblioteca estándar, solo las pestañas pedidas, sin frappe ni openpyxl."""

import datetime
import zipfile

import pytest

from kamra_uy.setup import xlsx
from kamra_uy.setup.tests import fabrica


def _libro(tmp_path, pestanas, **kw):
	ruta = tmp_path / "planilla.xlsx"
	fabrica.escribe_xlsx(ruta, pestanas, **kw)
	return ruta


def test_reads_only_the_named_sheets_and_never_parses_the_others(tmp_path) -> None:
	ruta = _libro(tmp_path, {"Uno": [["a", 1]], "Personal": fabrica.ROTA, "Dos": [[None, "b"]]})
	libro = xlsx.lee(ruta, ["Uno", "Dos"])
	assert libro.nombres == ["Uno", "Personal", "Dos"]
	assert libro.filas("Uno") == [(1, {"A": "a", "B": 1})]
	assert libro.filas("Dos") == [(1, {"B": "b"})]
	with pytest.raises(KeyError):
		libro.filas("Personal")


def test_a_missing_sheet_says_which_one_in_spanish(tmp_path) -> None:
	ruta = _libro(tmp_path, {"Uno": [["a"]]})
	with pytest.raises(xlsx.PlanillaIlegible, match="no tiene la pestaña «Dos»"):
		xlsx.lee(ruta, ["Uno", "Dos"])


def test_a_file_that_is_not_an_xlsx_is_a_clear_error(tmp_path) -> None:
	ruta = tmp_path / "x.xlsx"
	ruta.write_text("hola")
	with pytest.raises(xlsx.PlanillaIlegible, match="no es un .xlsx"):
		xlsx.lee(ruta, ["Uno"])


def test_shared_strings_keep_only_the_wanted_indices(tmp_path) -> None:
	ruta = _libro(tmp_path, {"Uno": [["solo de uno", "común"]], "Dos": [["común", "solo de dos"]]})
	with zipfile.ZipFile(ruta) as z:
		assert xlsx.cadenas_compartidas(z, {0, 1}) == {0: "solo de uno", 1: "común"}
		assert xlsx.cadenas_compartidas(z, {2}) == {2: "solo de dos"}


def test_numbers_are_int_when_integral_and_float_otherwise_and_booleans_are_bool(tmp_path) -> None:
	ruta = _libro(tmp_path, {"Uno": [[104, 12.5, True, "104"]]})
	assert xlsx.lee(ruta, ["Uno"]).filas("Uno") == [(1, {"A": 104, "B": 12.5, "C": True, "D": "104"})]


def test_inline_and_formula_strings_are_text(tmp_path) -> None:
	ruta = tmp_path / "x.xlsx"
	fabrica.escribe_xlsx(ruta, {"Uno": [["x"]]})
	main = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
	sheet = (
		f'<worksheet {main}><sheetData><row r="2"><c r="A2" t="inlineStr"><is><t>en línea</t></is></c>'
		'<c r="C2" t="str"><f>A2</f><v>de fórmula</v></c><c r="D2" t="e"><v>#N/A</v></c></row></sheetData></worksheet>'
	)
	otra = tmp_path / "y.xlsx"
	with zipfile.ZipFile(ruta) as src, zipfile.ZipFile(otra, "w") as dst:
		for n in src.namelist():
			dst.writestr(n, sheet if n.endswith("sheet1.xml") else src.read(n))
	assert xlsx.lee(otra, ["Uno"]).filas("Uno") == [(2, {"A": "en línea", "C": "de fórmula", "D": "#N/A"})]


@pytest.mark.parametrize("fecha1904", [False, True])
def test_dates_come_back_from_their_serial_with_the_book_epoch(tmp_path, fecha1904) -> None:
	dia = datetime.date(2026, 12, 20)
	ruta = _libro(tmp_path, {"Uno": [[dia]]}, fecha1904=fecha1904)
	libro = xlsx.lee(ruta, ["Uno"])
	(_, celdas), = libro.filas("Uno")
	assert libro.fecha(celdas["A"]) == dia


def test_column_letters_beyond_z(tmp_path) -> None:
	ruta = _libro(tmp_path, {"Uno": [[None] * 27 + ["AB"]]})
	assert xlsx.lee(ruta, ["Uno"]).filas("Uno") == [(1, {"AB": "AB"})]


@pytest.mark.parametrize("propio", [True, False])
def test_a_number_in_a_percent_cell_is_a_percentage(tmp_path, propio) -> None:
	# HR-290: la planilla del hotel guarda «100 %» como 1 con un formato propio «0 %»; Excel trae también el 9 («0%»)
	ruta = tmp_path / "p.xlsx"
	fabrica.escribe_xlsx(ruta, {"Hoja": [[fabrica.Porcentaje(1, propio=propio), 1, fabrica.Porcentaje(0.25, propio=propio)]]})
	(_, celdas), = xlsx.lee(ruta, ["Hoja"]).filas("Hoja")
	assert isinstance(celdas["A"], xlsx.Porcentaje) and celdas["A"] == 1
	assert not isinstance(celdas["B"], xlsx.Porcentaje) and celdas["B"] == 1
	assert isinstance(celdas["C"], xlsx.Porcentaje) and celdas["C"] == 0.25


def test_a_book_without_styles_has_no_percentages(tmp_path) -> None:
	ruta = tmp_path / "p.xlsx"
	fabrica.escribe_xlsx(ruta, {"Hoja": [[1, 0.5]]})
	(_, celdas), = xlsx.lee(ruta, ["Hoja"]).filas("Hoja")
	assert not any(isinstance(v, xlsx.Porcentaje) for v in celdas.values())
