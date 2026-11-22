"""Lector .xlsx mínimo, con la biblioteca estándar, para la carga de la planilla del hotel (HR-33).

Abre SOLO las pestañas que se le piden: las demás (en la planilla del hotel, las que llevan datos personales) ni se
parsean. De las cadenas compartidas, que Excel guarda juntas para todo el libro, se queda solo con las que usan esas
pestañas. Sin frappe ni openpyxl: lo usan las pruebas unitarias del gate rápido y la carga dentro de Kamra.
"""

import datetime
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET

_M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_REF = re.compile(r"^([A-Z]+)(\d+)$")


class Porcentaje(float):
	"""Un número de una celda con formato de porcentaje (HR-290): 1 = 100 %. Sigue siendo un float."""


_PCT_PROPIOS = (9, 10)  # los porcentajes de fábrica de Excel: «0%» y «0.00%»


def estilos_porcentaje(z: zipfile.ZipFile) -> set[int]:
	"""Los índices de estilo de celda (el `s` de cada <c>) cuyo formato es un porcentaje: el 9, el 10 o uno propio con
	«%» fuera de comillas. Sin styles.xml, ninguno."""
	if "xl/styles.xml" not in z.namelist():
		return set()
	try:
		raiz = ET.fromstring(z.read("xl/styles.xml"))
	except ET.ParseError:
		return set()
	pct = set(_PCT_PROPIOS)
	for nf in raiz.iter(f"{_M}numFmt"):
		codigo = re.sub(r'"[^"]*"|\\.', "", nf.get("formatCode") or "")
		if "%" in codigo:
			pct.add(int(nf.get("numFmtId")))
	xfs = raiz.find(f"{_M}cellXfs")
	if xfs is None:
		return set()
	return {i for i, xf in enumerate(xfs.findall(f"{_M}xf")) if int(xf.get("numFmtId") or 0) in pct}


class PlanillaIlegible(Exception):
	"""El fichero no se puede leer como la planilla: el mensaje va en español, para quien la rellena."""


def cadenas_compartidas(z: zipfile.ZipFile, wanted: set[int]) -> dict[int, str]:
	"""Las cadenas compartidas de índice en `wanted`; las demás se recorren sin guardarlas."""
	out: dict[int, str] = {}
	if not wanted or "xl/sharedStrings.xml" not in z.namelist():
		return out
	i = 0
	with z.open("xl/sharedStrings.xml") as fh:
		for _, el in ET.iterparse(fh):
			if el.tag == f"{_M}si":
				if i in wanted:
					out[i] = "".join(t.text or "" for t in el.iter(f"{_M}t"))
				i += 1
				el.clear()
	return out


def _numero(texto: str):
	n = float(texto)
	return int(n) if n.is_integer() else n


class Libro:
	"""Las pestañas leídas: `filas(nombre)` = [(número de fila, {columna: valor})], solo las celdas con valor."""

	def __init__(self, nombres: list[str], fecha1904: bool, filas: dict[str, list]):
		self.nombres = nombres
		self.fecha1904 = fecha1904
		self._filas = filas

	def filas(self, nombre: str) -> list[tuple[int, dict]]:
		return self._filas[nombre]

	def fecha(self, serie) -> datetime.date:
		"""Una fecha de Excel guardada como número de serie (con la época del libro)."""
		base = datetime.date(1904, 1, 1) if self.fecha1904 else datetime.date(1899, 12, 30)
		return base + datetime.timedelta(days=int(serie))


def lee(ruta, pestanas: list[str]) -> Libro:
	try:
		z = zipfile.ZipFile(ruta)
	except (zipfile.BadZipFile, OSError) as e:
		raise PlanillaIlegible(f"{ruta} no es un .xlsx que se pueda abrir ({e.__class__.__name__}).") from e
	with z:
		try:
			wb = ET.fromstring(z.read("xl/workbook.xml"))
			rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
		except (KeyError, ET.ParseError) as e:
			raise PlanillaIlegible(f"{ruta} no es un .xlsx de Excel válido (le falta el índice de pestañas).") from e
		destino = {r.get("Id"): r.get("Target") for r in rels.iter(f"{_PR}Relationship")}
		hojas = [(s.get("name"), destino.get(s.get(f"{_R}id"))) for s in wb.iter(f"{_M}sheet")]
		pr = wb.find(f"{_M}workbookPr")
		fecha1904 = pr is not None and pr.get("date1904", "false").lower() in ("1", "true")
		nombres = [n for n, _ in hojas]
		raices = {}
		for nombre in pestanas:
			target = dict(hojas).get(nombre)
			if target is None:
				raise PlanillaIlegible(f"La planilla no tiene la pestaña «{nombre}».")
			path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
			try:
				raices[nombre] = ET.fromstring(z.read(path))
			except (KeyError, ET.ParseError) as e:
				raise PlanillaIlegible(f"La pestaña «{nombre}» está dañada y no se puede leer.") from e
		wanted = {
			int(c.find(f"{_M}v").text)
			for raiz in raices.values()
			for c in raiz.iter(f"{_M}c")
			if c.get("t") == "s" and c.find(f"{_M}v") is not None
		}
		cadenas = cadenas_compartidas(z, wanted)
		pct = estilos_porcentaje(z)
	filas = {nombre: _filas(raiz, cadenas, pct) for nombre, raiz in raices.items()}
	return Libro(nombres, fecha1904, filas)


def _filas(raiz, cadenas: dict[int, str], pct: set[int] = frozenset()) -> list[tuple[int, dict]]:
	out = []
	for row in raiz.iter(f"{_M}row"):
		celdas = {}
		for c in row.findall(f"{_M}c"):
			m = _REF.match(c.get("r") or "")
			if not m:
				continue
			valor = _valor(c, cadenas)
			if pct and isinstance(valor, int | float) and not isinstance(valor, bool) and int(c.get("s") or 0) in pct:
				valor = Porcentaje(valor)
			if valor is not None and valor != "":
				celdas[m.group(1)] = valor
		if celdas:
			out.append((int(row.get("r")), celdas))
	return out


def _valor(c, cadenas: dict[int, str]):
	tipo = c.get("t")
	if tipo == "inlineStr":
		return "".join(t.text or "" for t in c.iter(f"{_M}t"))
	v = c.find(f"{_M}v")
	if v is None or v.text is None:
		return None
	if tipo == "s":
		return cadenas[int(v.text)]
	if tipo == "b":
		return v.text == "1"
	if tipo in ("str", "e", "d"):
		return v.text
	return _numero(v.text)
