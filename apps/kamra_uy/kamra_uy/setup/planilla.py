"""La planilla del hotel (HR-6) leída y validada para la carga en Kamra (HR-33). Sin frappe.

Hoy se cargan dos pestañas: «Habitaciones» y «Temporadas y precios». Las demás crecen aquí cuando el fundador las
rellene (una función `_<pestaña>` y su nombre en PESTANAS); las de datos personales no se abren nunca desde este
módulo salvo para la carga de su ficha, con su propia revisión. Las columnas se buscan por su cabecera, no por su
letra: insertar una columna no rompe la carga. Cada error dice pestaña, fila y columna, en español.

Para validar una planilla sin Kamra: `python3 -m kamra_uy.setup.planilla <planilla.xlsx>` (desde apps/kamra_uy).
"""

import dataclasses
import datetime
import re
import sys
import unicodedata

from kamra_uy.setup import xlsx

HABITACIONES = "Habitaciones"
TEMPORADAS = "Temporadas y precios"
PESTANAS = (HABITACIONES, TEMPORADAS)

_FECHA = re.compile(r"^\s*(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\s*$")
_FECHA_ISO = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$")
_PRECIO = re.compile(r"^\s*(\d+)(?:[.,](\d{1,2}))?\s*$")
_ENTERO = re.compile(r"^\s*(\d+)\s*$")
_MONEDAS = (
	(re.compile(r"US\$|U\$S|USD|d[oó]lar", re.I), "USD"),
	(re.compile(r"\$U|UYU|peso", re.I), "UYU"),
)


@dataclasses.dataclass(frozen=True)
class Nota:
	"""Un error o un aviso, con dónde está en la planilla."""

	pestana: str
	fila: int | None
	columna: str | None
	cabecera: str | None
	mensaje: str

	def texto(self) -> str:
		if self.fila is None:
			return f"{self.pestana}: {self.mensaje}"
		donde = f"{self.pestana}, fila {self.fila}"
		if self.columna:
			donde += f", columna {self.columna} ({self.cabecera})"
		return f"{donde}: {self.mensaje}"


@dataclasses.dataclass
class Habitacion:
	numero: str
	tipo: str
	camas: str | None
	adultos: int
	ninos: int | None
	piso: str | None
	notas: str | None
	fila: int


@dataclasses.dataclass
class Temporada:
	nombre: str
	desde: datetime.date
	hasta: datetime.date
	tipo: str
	precio: float
	minimo: int
	fila: int


@dataclasses.dataclass
class Tipo:
	nombre: str
	adultos: int | None
	ninos: int | None
	habitaciones: list
	precio_minimo: float | None = None


@dataclasses.dataclass
class Planilla:
	moneda: str | None = None
	tipos: dict = dataclasses.field(default_factory=dict)
	habitaciones: list = dataclasses.field(default_factory=list)
	temporadas: list = dataclasses.field(default_factory=list)
	sin_fechas: list = dataclasses.field(default_factory=list)
	avisos: list = dataclasses.field(default_factory=list)
	errores: list = dataclasses.field(default_factory=list)

	def recuento(self) -> dict:
		por_tipo: dict[str, int] = {}
		for h in self.habitaciones:
			por_tipo[h.tipo] = por_tipo.get(h.tipo, 0) + 1
		return {
			"habitaciones": len(self.habitaciones),
			"por_tipo": dict(sorted(por_tipo.items())),
			"temporadas": len({(t.nombre, t.desde, t.hasta) for t in self.temporadas}),
			"filas_de_precio": len(self.temporadas),
			"sin_fechas": list(self.sin_fechas),
		}


def _clave(texto: str) -> str:
	sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
	return " ".join(sin_tildes.casefold().split())


def _texto(valor) -> str | None:
	if valor is None:
		return None
	if isinstance(valor, float) and valor.is_integer():
		valor = int(valor)
	t = str(valor).strip()
	return t or None


class _Pestana:
	"""Las filas de datos de una pestaña, con las columnas localizadas por su cabecera."""

	def __init__(self, nombre: str, filas, requeridas: dict, opcionales: dict, visibles: dict, notas: list):
		self.nombre = nombre
		self.columnas: dict[str, str] = {}
		self.cabeceras: dict[str, str] = {}
		self.fila_cabecera = None
		self.datos = []
		buscadas = {**requeridas, **opcionales}
		for n, celdas in filas:
			if self.fila_cabecera is None:
				encontradas = {}
				for col, valor in celdas.items():
					t = _texto(valor)
					for campo, prefijo in buscadas.items():
						if t and _clave(t).startswith(prefijo) and campo not in encontradas:
							encontradas[campo] = (col, t)
				if all(c in encontradas for c in requeridas):
					self.fila_cabecera = n
					for campo, (col, t) in encontradas.items():
						self.columnas[campo] = col
						self.cabeceras[campo] = t
				continue
			self.datos.append((n, celdas))
		if self.fila_cabecera is None:
			nombres = [f"«{visibles[k]}»" for k in requeridas]
			lista = ", ".join(nombres[:-1]) + f" y {nombres[-1]}" if len(nombres) > 1 else nombres[0]
			notas.append(Nota(nombre, None, None, None, f"no encuentro la fila de cabecera con {lista}."))

	def valor(self, celdas: dict, campo: str):
		col = self.columnas.get(campo)
		return celdas.get(col) if col else None

	def nota(self, fila: int, campo: str | None, mensaje: str) -> Nota:
		col = self.columnas.get(campo) if campo else None
		return Nota(self.nombre, fila, col, self.cabeceras.get(campo) if col else None, mensaje)


# las cabeceras, por el comienzo de su texto sin tildes ni mayúsculas; el valor visible, para los mensajes
_HAB_REQ = {"numero": "numero", "tipo": "tipo", "adultos": "adultos max"}
_HAB_OPC = {"camas": "camas", "ninos": "ninos max", "piso": "piso", "notas": "notas"}
_TEM_REQ = {
	"temporada": "temporada",
	"desde": "desde",
	"hasta": "hasta",
	"tipo": "tipo de habitacion",
	"precio": "precio",
	"minimo": "estancia minima",
}
_HAB_VISIBLE = {"numero": "Número", "tipo": "Tipo", "adultos": "Adultos máx."}
_TEM_VISIBLE = {
	"temporada": "Temporada",
	"desde": "Desde",
	"hasta": "Hasta",
	"tipo": "Tipo de habitación",
	"precio": "Precio por noche",
	"minimo": "Estancia mínima",
}


def lee(ruta) -> Planilla:
	try:
		libro = xlsx.lee(ruta, list(PESTANAS))
	except xlsx.PlanillaIlegible as e:
		return Planilla(errores=[Nota("Planilla", None, None, None, str(e))])
	return analiza(libro)


def analiza(libro: xlsx.Libro) -> Planilla:
	p = Planilla()
	hab = _Pestana(HABITACIONES, libro.filas(HABITACIONES), _HAB_REQ, _HAB_OPC, _HAB_VISIBLE, p.errores)
	tem = _Pestana(TEMPORADAS, libro.filas(TEMPORADAS), _TEM_REQ, {}, _TEM_VISIBLE, p.errores)
	if hab.fila_cabecera is not None:
		_habitaciones(hab, p)
	if tem.fila_cabecera is not None:
		_temporadas(tem, libro, p)
	if hab.fila_cabecera is not None and tem.fila_cabecera is not None:
		_cuadra_tipos(hab, p)
	return p


def _entero(valor, minimo: int):
	"""(número, None) o (None, motivo) para una cantidad de personas o noches."""
	if isinstance(valor, bool):
		return None, "no"
	if isinstance(valor, int | float) and float(valor).is_integer():
		n = int(valor)
	else:
		m = _ENTERO.match(str(valor))
		if not m:
			return None, "no"
		n = int(m.group(1))
	return (n, None) if n >= minimo else (None, "no")


def _habitaciones(pest: _Pestana, p: Planilla) -> None:
	vistas: dict[str, int] = {}
	nombres: dict[str, str] = {}
	for fila, celdas in pest.datos:
		numero = _texto(pest.valor(celdas, "numero"))
		tipo = _texto(pest.valor(celdas, "tipo"))
		if tipo:
			tipo = nombres.setdefault(_clave(tipo), tipo)
			if tipo not in p.tipos:
				p.tipos[tipo] = Tipo(tipo, None, None, [])
		errores_antes = len(p.errores)
		if numero is None:
			p.errores.append(pest.nota(fila, "numero", "falta el número de la habitación."))
		elif numero in vistas:
			p.errores.append(
				pest.nota(fila, "numero", f"la habitación {numero} está repetida (también en la fila {vistas[numero]}).")
			)
		else:
			vistas[numero] = fila
		adultos, ninos = None, None
		bruto = pest.valor(celdas, "adultos")
		if bruto is None:
			p.errores.append(pest.nota(fila, "adultos", "falta cuántos adultos caben."))
		else:
			adultos, mal = _entero(bruto, 1)
			if mal:
				p.errores.append(pest.nota(fila, "adultos", f"«{_texto(bruto)}» no es un número de personas."))
		if tipo is None:
			p.errores.append(pest.nota(fila, "tipo", "falta el tipo de la habitación."))
		bruto = pest.valor(celdas, "ninos")
		if bruto is not None:
			ninos, mal = _entero(bruto, 0)
			if mal:
				p.errores.append(pest.nota(fila, "ninos", f"«{_texto(bruto)}» no es un número de personas."))
		if len(p.errores) > errores_antes:
			continue
		h = Habitacion(
			numero=numero,
			tipo=tipo,
			camas=_texto(pest.valor(celdas, "camas")),
			adultos=adultos,
			ninos=ninos,
			piso=_texto(pest.valor(celdas, "piso")),
			notas=_texto(pest.valor(celdas, "notas")),
			fila=fila,
		)
		t = p.tipos[tipo]
		if t.habitaciones:
			primera = t.habitaciones[0]
			for campo, unidad in (("adultos", "adultos"), ("ninos", "niños")):
				a, b = getattr(h, campo), getattr(primera, campo)
				if a != b:
					p.errores.append(
						pest.nota(
							fila,
							campo,
							f"la {h.numero} es {tipo} para {_cuantos(a)} {unidad} y la {primera.numero} "
							f"(fila {primera.fila}), para {_cuantos(b)}: en Kamra la capacidad es del tipo, no de la "
							"habitación.",
						)
					)
					break
			else:
				t.habitaciones.append(h)
				p.habitaciones.append(h)
			continue
		t.adultos, t.ninos = h.adultos, h.ninos
		t.habitaciones.append(h)
		p.habitaciones.append(h)
	for t in p.tipos.values():
		if t.habitaciones and t.ninos is None:
			p.avisos.append(
				Nota(
					HABITACIONES,
					t.habitaciones[0].fila,
					pest.columnas.get("ninos"),
					pest.cabeceras.get("ninos"),
					f"«Niños máx.» en blanco para {t.nombre}: la carga no lo toca (Kamra pone 1 al crear el tipo).",
				)
			)


def _cuantos(n) -> str:
	return "?" if n is None else str(n)


def _fecha(valor, libro: xlsx.Libro):
	"""(fecha, None), (None, "sin") si no hay fecha escrita, o (None, "mal") si parece una fecha imposible."""
	if valor is None:
		return None, "sin"
	if isinstance(valor, int | float) and not isinstance(valor, bool):
		return libro.fecha(valor), None
	t = str(valor)
	for patron, orden in ((_FECHA, (3, 2, 1)), (_FECHA_ISO, (1, 2, 3))):
		m = patron.match(t)
		if m:
			try:
				return datetime.date(*(int(m.group(i)) for i in orden)), None
			except ValueError:
				return None, "mal"
	return None, "sin"


def _precio(valor):
	if isinstance(valor, bool):
		return None
	if isinstance(valor, int | float):
		return valor
	limpio = re.sub(r"US\$|U\$S|\$U|\$|USD|UYU", "", str(valor), flags=re.I)
	m = _PRECIO.match(limpio)
	if not m:
		return None
	return float(f"{m.group(1)}.{m.group(2)}") if m.group(2) else int(m.group(1))


def _moneda(pest: _Pestana, p: Planilla) -> None:
	cabecera = pest.cabeceras.get("precio", "")
	for patron, moneda in _MONEDAS:
		if patron.search(cabecera):
			p.moneda = moneda
			return
	p.errores.append(
		pest.nota(pest.fila_cabecera, "precio", "la cabecera no dice la moneda (p. ej. «(US$)»).")
	)


def _dd(d: datetime.date) -> str:
	return d.strftime("%d-%m-%Y")


def _temporadas(pest: _Pestana, libro: xlsx.Libro, p: Planilla) -> None:
	_moneda(pest, p)
	tipos = {_clave(n): n for n in p.tipos}
	desconocidos: set[str] = set()
	bloques = []  # [nombre, desde, hasta, fila, {tipo: fila}, con_fechas]
	actual = None
	for fila, celdas in pest.datos:
		nombre = _texto(pest.valor(celdas, "temporada"))
		if nombre:
			desde, md = _fecha(pest.valor(celdas, "desde"), libro)
			hasta, mh = _fecha(pest.valor(celdas, "hasta"), libro)
			actual = {"nombre": nombre, "desde": desde, "hasta": hasta, "fila": fila, "tipos": {}, "ok": True}
			bloques.append(actual)
			for campo, f, motivo in (("desde", desde, md), ("hasta", hasta, mh)):
				if motivo == "mal":
					crudo = _texto(pest.valor(celdas, campo))
					p.errores.append(
						pest.nota(fila, campo, f"«{crudo}» no es una fecha (escríbela como 15/12/2026).")
					)
					actual["ok"] = False
			if actual["ok"] and (desde is None) != (hasta is None):
				falta = "hasta" if hasta is None else "desde"
				p.errores.append(pest.nota(fila, falta, f"a «{nombre}» le falta la fecha «{pest.cabeceras[falta]}»."))
				actual["ok"] = False
			if actual["ok"] and desde is None:
				actual["ok"] = None  # sin fechas: fuera de la carga, con aviso
				p.sin_fechas.append(nombre)
				p.avisos.append(
					pest.nota(
						fila,
						"desde",
						f"«{nombre}» no tiene fechas: queda fuera de la carga hasta que las tenga (pregúntalo).",
					)
				)
			elif actual["ok"] and hasta < desde:
				p.errores.append(pest.nota(fila, "hasta", f"«{nombre}» acaba antes de empezar."))
				actual["ok"] = False
		elif actual is None:
			p.errores.append(pest.nota(fila, "temporada", "falta el nombre de la temporada."))
			continue
		if actual["ok"] is None:
			continue
		tipo_bruto = _texto(pest.valor(celdas, "tipo"))
		if tipo_bruto is None:
			p.errores.append(pest.nota(fila, "tipo", "falta el tipo de habitación."))
			actual["ok"] = False
			continue
		tipo = tipos.get(_clave(tipo_bruto))
		if tipo is None:
			if _clave(tipo_bruto) not in desconocidos:
				desconocidos.add(_clave(tipo_bruto))
				hay = sorted(p.tipos)
				lista = ", ".join(hay[:-1]) + f" y {hay[-1]}" if len(hay) > 1 else "".join(hay)
				p.errores.append(
					pest.nota(
						fila,
						"tipo",
						f"«{tipo_bruto}» no es un tipo de la pestaña {HABITACIONES} (allí hay {lista}): usa el mismo "
						"nombre en las dos.",
					)
				)
			actual["ok"] = False
			continue
		if tipo in actual["tipos"]:
			p.errores.append(
				pest.nota(
					fila,
					"tipo",
					f"«{tipo}» sale dos veces en «{actual['nombre']}» (también en la fila {actual['tipos'][tipo]}).",
				)
			)
			actual["ok"] = False
			continue
		actual["tipos"][tipo] = fila
		bruto = pest.valor(celdas, "precio")
		precio = _precio(bruto) if bruto is not None else None
		if bruto is None:
			p.errores.append(pest.nota(fila, "precio", "falta el precio."))
		elif precio is None:
			p.errores.append(pest.nota(fila, "precio", f"«{_texto(bruto)}» no es un precio."))
		elif precio <= 0:
			p.errores.append(pest.nota(fila, "precio", "el precio tiene que ser mayor que 0."))
			precio = None
		bruto = pest.valor(celdas, "minimo")
		minimo, mal = (1, None) if bruto is None else _entero(bruto, 1)
		if mal:
			p.errores.append(pest.nota(fila, "minimo", f"«{_texto(bruto)}» no es un número de noches."))
		if precio is None or mal or actual["ok"] is False:
			actual["ok"] = False
			continue
		p.temporadas.append(
			Temporada(actual["nombre"], actual["desde"], actual["hasta"], tipo, precio, minimo, fila)
		)
	_cuadra_bloques(pest, bloques, p)


def _cuadra_bloques(pest: _Pestana, bloques: list, p: Planilla) -> None:
	con_fechas = [b for b in bloques if b["desde"] and b["hasta"] and b["hasta"] >= b["desde"]]
	for b in con_fechas:
		for tipo in p.tipos:
			if p.tipos[tipo].habitaciones and tipo not in b["tipos"] and b["ok"] is not False:
				p.errores.append(pest.nota(b["fila"], "tipo", f"a «{b['nombre']}» le falta el precio de {tipo}."))
	orden = sorted(con_fechas, key=lambda b: (b["desde"], b["fila"]))
	for antes, despues in zip(orden, orden[1:]):
		if despues["desde"] <= antes["hasta"]:
			p.errores.append(
				pest.nota(
					despues["fila"],
					"desde",
					f"«{despues['nombre']}» se pisa con «{antes['nombre']}» (fila {antes['fila']}) el "
					f"{_dd(despues['desde'])}: Kamra no sabría qué precio cobrar.",
				)
			)
		elif (despues["desde"] - antes["hasta"]).days > 1:
			hueco_desde = antes["hasta"] + datetime.timedelta(days=1)
			hueco_hasta = despues["desde"] - datetime.timedelta(days=1)
			p.avisos.append(
				pest.nota(
					despues["fila"],
					"desde",
					f"entre el {_dd(hueco_desde)} y el {_dd(hueco_hasta)} no hay temporada: esas noches irían al precio "
					"base del tipo.",
				)
			)
	for nombre, t in p.tipos.items():
		precios = [s.precio for s in p.temporadas if s.tipo == nombre]
		t.precio_minimo = min(precios) if precios else None


def _cuadra_tipos(hab: _Pestana, p: Planilla) -> None:
	con_precio = {t.tipo for t in p.temporadas}
	for nombre, t in p.tipos.items():
		if t.habitaciones and nombre not in con_precio:
			p.errores.append(
				hab.nota(t.habitaciones[0].fila, "tipo", f"«{nombre}» no tiene precio en «{TEMPORADAS}».")
			)


def main(argv: list[str]) -> int:
	if len(argv) != 1:
		print("uso: python3 -m kamra_uy.setup.planilla <planilla.xlsx>", file=sys.stderr)
		return 2
	p = lee(argv[0])
	print(f"moneda: {p.moneda or '?'} · {p.recuento()}")
	for n in p.avisos:
		print("AVISO", n.texto())
	for n in p.errores:
		print("ERROR", n.texto())
	print("planilla VÁLIDA" if not p.errores else f"planilla CON {len(p.errores)} ERRORES")
	return 0 if not p.errores else 1


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
