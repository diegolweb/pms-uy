"""La planilla del hotel (HR-6) leída y validada para la carga en Kamra (HR-33). Sin frappe.

Hoy se cargan tres pestañas: «Habitaciones», «Temporadas y precios» y «Seña y cancelación» (HR-290). Las demás crecen aquí cuando el fundador las
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
SENA = "Seña y cancelación"
PESTANAS = (HABITACIONES, TEMPORADAS, SENA)

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
class Condiciones:
	"""La pestaña «Seña y cancelación» (HR-290). None = en blanco o sin la pregunta: la carga no lo toca. Los cargos, con
	los valores de Kamra (`Property.cancellation_fee` y `no_show_charge`): «First Night», «Full Stay» o «None»."""

	pago_total: bool | None = None
	retencion_minutos: int | None = None
	entrada: str | None = None  # «14:00:00», como un Time de Frappe
	salida: str | None = None
	cancelacion_gratis_dias: int | None = None
	cargo_cancelacion: str | None = None
	cargo_no_show: str | None = None
	por_habitacion: bool | None = None  # los niños se cobran por habitación, no por persona
	mascotas: bool | None = None  # False: no se aceptan


@dataclasses.dataclass
class Planilla:
	moneda: str | None = None
	condiciones: Condiciones = dataclasses.field(default_factory=Condiciones)
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
	sen = _Pestana(SENA, libro.filas(SENA), _SEN_REQ, {}, _SEN_VISIBLE, p.errores)
	if sen.fila_cabecera is not None:
		_condiciones(sen, p)
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


_SEN_REQ = {"pregunta": "pregunta", "respuesta": "respuesta"}
_SEN_VISIBLE = {"pregunta": "Pregunta", "respuesta": "Respuesta"}

# las preguntas de «Seña y cancelación», por el comienzo de su texto sin tildes ni mayúsculas, y cómo se nombran en los
# avisos («la carga no toca …»)
_PREGUNTAS = (
	("cobro_alta", "sena en temporada alta", "la seña de temporada alta"),
	("cobro_resto", "sena el resto", "la seña del resto del año"),
	("retencion", "tiempo que se guarda", "la retención para pagar"),
	("entrada", "hora de entrada", "la hora de entrada"),
	("salida", "hora de salida", "la hora de salida"),
	("dias", "cancelacion gratis", "la cancelación gratis"),
	("cancela", "si cancela despues", "lo que pierde quien cancela tarde"),
	("no_show", "si no llega", "el cargo del no-show"),
	("ninos", "ninos", "el cobro de los niños"),
	("otras", "otras reglas", "las mascotas"),
)
_HORA = re.compile(r"^\s*(\d{1,2})(?:\s*[:.h]\s*(\d{2}))?\s*(?:h|hs|horas)?\s*$")
_RETENCION = re.compile(r"^\s*(\d+)\s*(min|mins|minutos?|h|hs|horas?)?\s*$")
_DIAS = re.compile(r"^\s*(\d+)\s*(dias?)?\s*$")
_UNA_NOCHE = re.compile(r"^(1 noche|una noche|la primera noche|primera noche|el importe de la primera noche)$")
_ESTADIA = re.compile(r"^(toda la estadia|la estadia entera|la estadia completa|el total|total|toda la reserva)$")
_NADA = re.compile(r"^(nada|ninguno|ninguna|no se cobra|sin cargo)$")
_PCT_TEXTO = re.compile(r"^(\d+(?:[.,]\d+)?)\s*%$")
_SIN_MASCOTAS = re.compile(r"^(no se aceptan mascotas|no se admiten mascotas|sin mascotas|no se permiten mascotas)$")


def _condiciones(pest: _Pestana, p: Planilla) -> None:
	c = p.condiciones
	filas: dict[str, tuple[int, object]] = {}
	for fila, celdas in pest.datos:
		pregunta = _texto(pest.valor(celdas, "pregunta"))
		if not pregunta:
			continue
		for campo, prefijo, _ in _PREGUNTAS:
			if _clave(pregunta).startswith(prefijo) and campo not in filas:
				filas[campo] = (fila, pest.valor(celdas, "respuesta"))
				break
	for campo, _, nombre in _PREGUNTAS:
		if campo not in filas:
			p.avisos.append(Nota(SENA, None, None, None, f"no encuentro la pregunta de {nombre}: la carga no la toca."))
			continue
		fila, bruto = filas[campo]
		if _texto(bruto) is None:
			p.avisos.append(pest.nota(fila, "respuesta", f"en blanco: la carga no toca {nombre}."))
			continue
		_respuesta(pest, p, c, campo, fila, bruto)
	if c.pago_total is False:
		c.pago_total = None


def _respuesta(pest: _Pestana, p: Planilla, c: Condiciones, campo: str, fila: int, bruto) -> None:
	crudo = _texto(bruto)
	clave = _clave(crudo).rstrip(".")

	def error(mensaje: str) -> None:
		p.errores.append(pest.nota(fila, "respuesta", mensaje))

	def no_lo_lee(texto: str) -> None:
		p.avisos.append(pest.nota(fila, "respuesta", f"«{texto}»: la carga no lo lee; ponlo en Kamra si hace falta."))

	if campo in ("cobro_alta", "cobro_resto"):
		if clave.startswith("pago total") or clave in ("total", "100", "100 %", "100%"):
			if c.pago_total is not False:
				c.pago_total = True
			return
		c.pago_total = False
		error(f"«{crudo}»: la carga solo sabe cobrar el total de la estadía al reservar (D-HR-38); para cobrar una seña, "
		      "pregúntalo antes de cargar.")
	elif campo == "retencion":
		m = _RETENCION.match(clave)
		if not m or int(m.group(1)) < 1:
			error(f"«{crudo}» no es un tiempo (escríbelo como 120 minutos).")
			return
		c.retencion_minutos = int(m.group(1)) * (60 if (m.group(2) or "m").startswith("h") else 1)
	elif campo in ("entrada", "salida"):
		hora = _hora(bruto, clave)
		if hora is None:
			error(f"«{crudo}» no es una hora (escríbela como 14:00).")
			return
		setattr(c, campo, hora)
	elif campo == "dias":
		m = _DIAS.match(clave)
		if not m:
			error(f"«{crudo}» no es un número de días.")
			return
		c.cancelacion_gratis_dias = int(m.group(1))
	elif campo in ("cancela", "no_show"):
		cargo = _cargo(bruto, clave)
		if cargo == "?":
			error(f"«{crudo}»: ¿una noche o el 100 %? Escribe «100 %», «la primera noche» o «toda la estadía».")
			return
		if cargo is None:
			que = f"{float(bruto) * 100:g} %" if isinstance(bruto, xlsx.Porcentaje) else crudo
			error(f"«{que}»: Kamra solo sabe cobrar la primera noche, el 100 % (toda la estadía) o nada.")
			return
		setattr(c, "cargo_cancelacion" if campo == "cancela" else "cargo_no_show", cargo)
	elif campo == "ninos":
		if clave in ("por habitacion", "se cobra por habitacion") or clave.startswith("se cobra por habitacion, no por persona"):
			c.por_habitacion = True
		else:
			no_lo_lee(crudo)
	elif campo == "otras":
		resto = []
		for frase in re.split(r"[.;\n]+", crudo):
			if not frase.strip():
				continue
			if _SIN_MASCOTAS.match(_clave(frase)):
				c.mascotas = False
			else:
				resto.append(frase.strip())
		if resto:
			no_lo_lee(". ".join(resto))


def _cargo(bruto, clave: str) -> str | None:
	"""El cargo de Kamra («First Night», «Full Stay» o «None»), "?" si es un número que puede ser noches o porcentaje, o
	None si Kamra no lo sabe cobrar. El fundador (22-11): «No dice 1, dice 100%»: su celda guarda 1 con formato «0 %»."""
	if isinstance(bruto, xlsx.Porcentaje):
		pct = round(float(bruto) * 100, 4)
	elif isinstance(bruto, int | float) and not isinstance(bruto, bool):
		pct = {0: 0, 100: 100}.get(bruto, "?")
	elif _PCT_TEXTO.match(clave):
		pct = float(_PCT_TEXTO.match(clave).group(1).replace(",", "."))
	elif clave in ("0", "100"):
		pct = int(clave)
	elif re.match(r"^\d+$", clave):
		return "?"
	else:
		return "First Night" if _UNA_NOCHE.match(clave) else "Full Stay" if _ESTADIA.match(clave) else (
			"None" if _NADA.match(clave) else None)
	if pct == "?":
		return "?"
	return {0: "None", 100: "Full Stay"}.get(pct)


def _hora(bruto, clave: str) -> str | None:
	if isinstance(bruto, int | float) and not isinstance(bruto, bool) and 0 <= bruto < 1:
		minutos = round(bruto * 24 * 60)
		return f"{minutos // 60:02d}:{minutos % 60:02d}:00"
	m = _HORA.match(clave)
	if not m or int(m.group(1)) > 23 or int(m.group(2) or 0) > 59:
		return None
	return f"{int(m.group(1)):02d}:{int(m.group(2) or 0):02d}:00"


def main(argv: list[str]) -> int:
	if len(argv) != 1:
		print("uso: python3 -m kamra_uy.setup.planilla <planilla.xlsx>", file=sys.stderr)
		return 2
	p = lee(argv[0])
	print(f"moneda: {p.moneda or '?'} · {p.recuento()}")
	print(f"condiciones: {dataclasses.asdict(p.condiciones)}")
	for n in p.avisos:
		print("AVISO", n.texto())
	for n in p.errores:
		print("ERROR", n.texto())
	print("planilla VÁLIDA" if not p.errores else f"planilla CON {len(p.errores)} ERRORES")
	return 0 if not p.errores else 1


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
