"""Planillas .xlsx INVENTADAS para probar la carga (HR-33), escritas con la biblioteca estándar (sin openpyxl).

Ningún dato de aquí es del hotel: los números de habitación, los precios y las fechas son de mentira, y así deben
seguir (apps/kamra_uy se publica, D-HR-24). La usan las unitarias (pytest, sin frappe), la de integración de
`kamra_uy/setup/tests/test_carga.py` y el check e2e `tests/e2e/checks/hr33-carga.sh`:

	python3 -I kamra_uy/setup/tests/fabrica.py <salida.xlsx> [precio_doble]
"""

import datetime
import sys
import zipfile
from xml.sax.saxutils import escape

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_EPOCH = datetime.date(1899, 12, 30)
_EPOCH_1904 = datetime.date(1904, 1, 1)

ROTA = object()


class Porcentaje(float):
	"""En `escribe_xlsx`, un número con formato de porcentaje (HR-290): `propio` usa uno propio «0 %» como el de la
	planilla del hotel; si no, el 9 de Excel («0%»). 1 = 100 %."""

	def __new__(cls, valor: float, propio: bool = True):
		out = super().__new__(cls, valor)
		out.propio = propio
		return out
  # en `escribe_xlsx`, una pestaña cuyo XML es basura: prueba que la carga no la abre


def _col(n: int) -> str:
	out = ""
	while n:
		n, r = divmod(n - 1, 26)
		out = chr(65 + r) + out
	return out


def escribe_xlsx(ruta, pestanas: dict, fecha1904: bool = False) -> None:
	"""`pestanas` = {nombre: filas | ROTA}; cada fila, una lista de valores (str, int, float, bool, date o None).
	Los textos van a sharedStrings (como los guarda Excel); las fechas, como número de serie."""
	strings: list[str] = []
	index: dict[str, int] = {}
	epoch = _EPOCH_1904 if fecha1904 else _EPOCH
	sheets_xml = []
	hay_estilos = False
	for filas in pestanas.values():
		if filas is ROTA:
			sheets_xml.append("<<esto no es XML")
			continue
		rows = []
		for r, fila in enumerate(filas, start=1):
			cells = []
			for c, valor in enumerate(fila, start=1):
				ref = f"{_col(c)}{r}"
				if valor is None:
					continue
				if isinstance(valor, Porcentaje):
					hay_estilos = True
					cells.append(f'<c r="{ref}" s="{1 if valor.propio else 2}"><v>{float(valor):g}</v></c>')
				elif isinstance(valor, bool):
					cells.append(f'<c r="{ref}" t="b"><v>{int(valor)}</v></c>')
				elif isinstance(valor, datetime.date):
					cells.append(f'<c r="{ref}"><v>{(valor - epoch).days}</v></c>')
				elif isinstance(valor, int | float):
					cells.append(f'<c r="{ref}"><v>{valor}</v></c>')
				else:
					texto = str(valor)
					if texto not in index:
						index[texto] = len(strings)
						strings.append(texto)
					cells.append(f'<c r="{ref}" t="s"><v>{index[texto]}</v></c>')
			rows.append(f'<row r="{r}">{"".join(cells)}</row>')
		sheets_xml.append(f'<worksheet xmlns="{_MAIN}"><sheetData>{"".join(rows)}</sheetData></worksheet>')
	nombres = list(pestanas)
	with zipfile.ZipFile(ruta, "w", zipfile.ZIP_DEFLATED) as z:
		z.writestr(
			"[Content_Types].xml",
			'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
			'<Default Extension="xml" ContentType="application/xml"/></Types>',
		)
		z.writestr(
			"xl/workbook.xml",
			f'<workbook xmlns="{_MAIN}" xmlns:r="{_REL}"><workbookPr date1904="{str(fecha1904).lower()}"/><sheets>'
			+ "".join(
				f'<sheet name="{escape(n, {chr(34): "&quot;"})}" sheetId="{i}" r:id="rId{i}"/>'
				for i, n in enumerate(nombres, 1)
			)
			+ "</sheets></workbook>",
		)
		z.writestr(
			"xl/_rels/workbook.xml.rels",
			'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
			+ "".join(
				f'<Relationship Id="rId{i}" Type="{_REL}/worksheet" Target="worksheets/sheet{i}.xml"/>'
				for i in range(1, len(nombres) + 1)
			)
			+ "</Relationships>",
		)
		for i, xml in enumerate(sheets_xml, 1):
			z.writestr(f"xl/worksheets/sheet{i}.xml", xml)
		if hay_estilos:
			z.writestr(
				"xl/styles.xml",
				f'<styleSheet xmlns="{_MAIN}"><numFmts count="1"><numFmt numFmtId="167" formatCode="0\\\u00a0%"/>'
				'</numFmts><cellXfs count="3"><xf numFmtId="0"/><xf numFmtId="167"/><xf numFmtId="9"/></cellXfs>'
				"</styleSheet>",
			)
		z.writestr(
			"xl/sharedStrings.xml",
			f'<sst xmlns="{_MAIN}" count="{len(strings)}" uniqueCount="{len(strings)}">'
			+ "".join(f"<si><t>{escape(s)}</t></si>" for s in strings)
			+ "</sst>",
		)


HAB_CABECERA = ["Número", "Tipo", "Camas", "Adultos máx.", "Niños máx.", "Piso", "Notas"]
TEM_CABECERA = [
	"Temporada",
	"Desde",
	"Hasta",
	"Tipo de habitación",
	"Precio por noche (US$)",
	"Estancia mínima (noches)",
	"Notas",
]

TEM_PRECIO2 = "Precio para 2 (US$)"  # la columna opcional de D-HR-81


def con_precio2(filas: list[list], precios2: dict, antes: bool = False) -> tuple[list, list[list]]:
	"""«Temporadas y precios» con la columna «Precio para 2 (US$)» (D-HR-81): la cabecera y las filas, con
	`precios2` = {índice de la fila en `filas`: valor} y vacía en las demás. La columna va tras «Precio por noche», o
	delante con `antes` (gana igual: casa más largo)."""
	donde = 4 if antes else 5
	cabecera = [*TEM_CABECERA[:donde], TEM_PRECIO2, *TEM_CABECERA[donde:]]
	return cabecera, [[*f[:donde], precios2.get(i), *f[donde:]] for i, f in enumerate(filas)]


# La planilla inventada: 6 habitaciones (Doble 2, Triple 3, Apartamento 1) y 3 temporadas contiguas de 2030, más una
# sin fechas. Los mismos nombres de tipo que el hotel (no son datos sensibles); ni sus números ni sus precios.
HABITACIONES = [
	["801", "Doble", "2 camas individuales", 2, None, 8, "Vista inventada A"],
	["802", "Doble", "2 camas individuales", 2, None, 8, None],
	["803", "Triple", "1 cama doble y 1 individual", 3, None, 8, "Vista inventada B"],
	["804", "Triple", "1 cama doble y 1 individual", 3, None, 8, None],
	["805", "Triple", "1 cama doble y 1 individual", 3, None, 8, None],
	["806", "Apartamento", "1 cama doble y 3 individuales", 5, None, 8, None],
]
D = datetime.date


def temporadas(precio_doble: float = 1001) -> list[list]:
	return [
		["Alta inventada", D(2030, 1, 1), D(2030, 1, 10), "Doble", precio_doble, 3, None],
		[None, None, None, "Triple", 1502, 3, None],
		[None, None, None, "Apartamento", 2003, 3, None],
		["Media inventada", D(2030, 1, 11), D(2030, 1, 20), "Doble", 801, 2, None],
		[None, None, None, "Triple", 1202, 2, None],
		[None, None, None, "Apartamento", 1603, 2, None],
		["Baja inventada", D(2030, 1, 21), D(2030, 2, 28), "Doble", 501, None, None],
		[None, None, None, "Triple", 702, 1, None],
		[None, None, None, "Apartamento", 903, 1, None],
		["Julio inventado", "Fechas por Confirmar", None, "Doble", 601, 2, None],
		[None, None, None, "Triple", 802, 2, None],
		[None, None, None, "Apartamento", 1003, 2, None],
	]


SENA_CABECERA = ["Pregunta", "Respuesta", "Notas"]


def sena() -> list[list]:
	"""La pestaña «Seña y cancelación» inventada (HR-290): las mismas preguntas que la del hotel, con respuestas de
	mentira (ni sus horas, ni sus días, ni su retención)."""
	return [
		["Seña en temporada alta (del 15-dic al 15-mar)", "Pago total de la estadía", None],
		["Seña el resto del año", "Pago total de la estadía", None],
		["Tiempo que se guarda la habitación para pagar la seña", "45 minutos", None],
		["Hora de entrada (check-in)", "15:30", None],
		["Hora de salida (check-out)", "10:15", None],
		["Cancelación gratis hasta cuántos días antes de la llegada", 9, None],
		["Si cancela después de ese plazo, ¿qué pierde?", Porcentaje(1), None],
		["Si no llega (no-show), ¿qué se cobra?", "La primera noche", None],
		["Niños: ¿hasta qué edad no pagan o pagan menos?", "Se cobra por habitación, no por persona.", None],
		["Otras reglas (mascotas, cama extra, salida tardía…)", "No se aceptan mascotas", None],
	]


RES_CABECERA = [
	"Código",
	"Plataforma",
	"Huésped",
	"Teléfono",
	"Correo",
	"Llegada",
	"Salida",
	"Tipo de habitación",
	"Habitación",
	"Adultos",
	"Niños",
	"Total (US$)",
	"Estado",
	"Notas",
]


def reservas() -> list[list]:
	"""Reservas INVENTADAS (HR-33), todas de 2030: ni los huéspedes, ni los códigos, ni los importes existen. Dos de
	Booking.com (una con el código con puntos, como lo enseña la extranet, y sin total: la precia Kamra), una de Expedia
	de dos habitaciones (el mismo código en dos filas), una de Airbnb con habitación, una directa por teléfono con total y
	una cancelada, que no se carga: 6 reservas que cargar."""
	return [
		["4100000001", "Booking.com", "Huésped Inventado Uno", "+598 99 000 001", "uno@example.test", D(2030, 1, 3),
		 D(2030, 1, 6), "Doble", None, 2, None, 2400, None, "Llega tarde (inventado)"],
		["4100.000.002", "Booking", "Huésped Inventado Dos", None, None, D(2030, 1, 12), D(2030, 1, 14), "Triple", None,
		 2, 1, None, "Confirmada", None],
		["EXP-INV-77", "Expedia", "Huésped Inventado Tres", None, None, D(2030, 1, 22), D(2030, 1, 25), "Triple", None,
		 3, None, 1500, None, None],
		["EXP-INV-77", "Expedia", "Huésped Inventado Tres", None, None, D(2030, 1, 22), D(2030, 1, 25), "Doble", None,
		 2, None, 1200, None, None],
		["HMINVENT04", "Airbnb", "Huésped Inventado Cuatro", None, "cuatro@example.test", D(2030, 2, 1), D(2030, 2, 8),
		 "Apartamento", "806", 4, 1, None, None, None],
		["T-0005", "Teléfono", "Huésped Inventado Cinco", "+598 99 000 005", None, D(2030, 1, 15), D(2030, 1, 17),
		 "Doble", "802", 2, None, 1000, None, None],
		["4100000006", "Booking.com", "Huésped Inventado Seis", None, None, D(2030, 1, 3), D(2030, 1, 5), "Doble", None,
		 2, None, None, "Cancelada", None],
	]


def pestanas(habitaciones=None, temporadas_=None, extra: dict | None = None, sena_=None, reservas_=None,
             tem_cabecera=None) -> dict:
	"""Las seis pestañas de la planilla real, con las dos de datos personales ROTAS: abrirlas tumba la prueba. Con
	`reservas_` (filas bajo RES_CABECERA), la de reservas trae esas reservas inventadas; «Personal» sigue rota. Con
	`tem_cabecera`, «Temporadas y precios» lleva esa cabecera (ver `con_precio2`)."""
	out = {
		"Léeme": [["Planilla inventada para pruebas"], ["Precios en dólares."]],
		"Habitaciones": [["1. Habitaciones"], ["Una fila por habitación."], HAB_CABECERA, *(habitaciones or HABITACIONES)],
		"Temporadas y precios": [
			["2. Temporadas y precios"],
			["Una fila por temporada y tipo."],
			tem_cabecera or TEM_CABECERA,
			*(temporadas_ if temporadas_ is not None else temporadas()),
		],
		"Seña y cancelación": [
			["3. Seña y cancelación"],
			["Lo que ya acordamos va escrito en amarillo."],
			SENA_CABECERA,
			*(sena_ if sena_ is not None else sena()),
		],
		"Reservas desde 15-nov": ROTA if reservas_ is None else [
			["5. Reservas desde 15-nov"],
			["Una fila por habitación reservada."],
			RES_CABECERA,
			*reservas_,
		],
		"Personal": ROTA,
	}
	out.update(extra or {})
	return out


def planilla_inventada(ruta, precio_doble: float = 1001, con_reservas: bool = False) -> None:
	escribe_xlsx(ruta, pestanas(temporadas_=temporadas(precio_doble), reservas_=reservas() if con_reservas else None))


if __name__ == "__main__":
	planilla_inventada(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 1001)
