"""El plan de la carga (HR-33): la planilla frente a lo que ya hay en Kamra, sin frappe.

Claves naturales: la propiedad por su nombre; el tipo por su nombre (sin distinguir mayúsculas ni tildes); la
habitación por su número; la temporada por tipo, nombre y fechas. Cada registro sale como `crear`, `cambiar`, `igual`
o `retirar` (una temporada de la planilla que en Kamra quedó con otras fechas, o que ya no tiene fechas: se desactiva,
no se borra). Lo de Kamra que la planilla no trae se avisa y no se toca.

Reglas de cobro (ADDENDUM de KICKOFF-HR-220, 20-11, fundador: «el hotel no cobra reserva, se realiza el pago al
completo»): la propiedad en «Full online» con deposit_pct 100, y ninguna temporada de la carga con advance_percent,
porque una temporada con porcentaje manda sobre «Full online» (kamra/public_api.py, `_advance_terms`).

HR-290: la pestaña «Seña y cancelación» pone en la propiedad la retención, las horas de entrada y salida, la cancelación
gratis, lo que cuesta cancelar tarde o no llegar y las mascotas; lo que está en blanco no se toca. Su cobro tiene que ser
el total (planilla.py lo valida). La capacidad de cada tipo es de PERSONAS (el fundador, 22-11): adultos hasta la
capacidad, niños hasta la capacidad menos 1 (siempre un adulto) salvo que «Niños máx.» diga otro número, y el total hasta
la capacidad (`max_total_occupants`, que Kamra aplica en `pricing.capacity_overflow`). Con «se cobra por habitación», un
niño o un adulto más no suma nada al precio.
"""

import dataclasses
import datetime
import unicodedata

from kamra_uy.pure.contingency import HOTEL_TIMEZONE
from kamra_uy.setup.planilla import Planilla

PAIS = "Uruguay"
MODO_DE_COBRO = "Full online"
DEPOSITO_PCT = 100
RETENCION_MINUTOS = 120  # el valor por defecto de Kamra (Property.hold_minutes)
ZONAS_POR_DEFECTO = ("", None, "Asia/Kolkata")  # sin zona o la que Kamra pone de fábrica (India)

OPS = ("crear", "cambiar", "igual", "retirar")


@dataclasses.dataclass
class Estado:
	"""Lo que hay en Kamra para la propiedad, en diccionarios con los nombres de campo de sus doctypes."""

	propiedad: dict | None
	reservas: int
	tipos: list
	habitaciones: list
	temporadas: list
	tramos_cancelacion: int = 0  # filas de Property.cancellation_tiers: si las hay, mandan sobre free_cancel_days


@dataclasses.dataclass
class Accion:
	doctype: str
	op: str
	valores: dict  # los campos a escribir; en Room y Season, `room_type` es el NOMBRE de la planilla (ver `tipo`)
	nombre: str | None = None  # el registro de Kamra que se cambia o retira; None al crear
	antes: dict = dataclasses.field(default_factory=dict)
	tipo: str | None = None  # el tipo de la planilla al que pertenece


@dataclasses.dataclass
class Plan:
	propiedad: str
	planilla: Planilla
	estado: Estado
	acciones: list = dataclasses.field(default_factory=list)
	avisos: list = dataclasses.field(default_factory=list)
	errores: list = dataclasses.field(default_factory=list)

	def recuento(self) -> dict:
		out: dict[str, dict[str, int]] = {}
		for a in self.acciones:
			por_op = out.setdefault(a.doctype, {})
			por_op[a.op] = por_op.get(a.op, 0) + 1
		return out

	def texto(self, aplicado: bool) -> str:
		return _texto(self, aplicado)


def _clave(texto: str | None) -> str:
	sin_tildes = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
	return " ".join(sin_tildes.casefold().split())


def _igual(a, b) -> bool:
	if isinstance(a, int | float) or isinstance(b, int | float):
		try:
			return abs(float(a or 0) - float(b or 0)) < 0.005
		except (TypeError, ValueError):
			return False
	if isinstance(a, datetime.date) or isinstance(b, datetime.date):
		return str(a) == str(b)
	return (a or None) == (b or None)


def _diferencias(actual: dict, deseado: dict) -> tuple[dict, dict]:
	valores = {k: v for k, v in deseado.items() if not _igual(actual.get(k), v)}
	return valores, {k: actual.get(k) for k in valores}


def codigo(nombre: str, usados: set[str]) -> str:
	"""El código de tipo de Kamra (solo letras latinas y cifras) a partir del nombre, sin repetir uno de la casa."""
	base = "".join(ch for ch in _clave(nombre).upper() if ch.isascii() and ch.isalnum())[:20] or "TIPO"
	out, n = base, 2
	while out.casefold() in usados:
		out, n = f"{base}{n}", n + 1
	return out


def planea(planilla: Planilla, estado: Estado, propiedad: str | None = None) -> Plan:
	nombre = propiedad or (estado.propiedad or {}).get("name") or "?"
	pl = Plan(nombre, planilla, estado)
	pl.avisos.extend(n.texto() for n in planilla.avisos)
	if planilla.errores:
		pl.errores.extend(n.texto() for n in planilla.errores)
		return pl
	if estado.propiedad is None:
		pl.errores.append(
			f"La propiedad «{nombre}» no existe en Kamra: créala con el asistente de Kamra y vuelve a correr la carga."
		)
		return pl
	_propiedad(pl)
	if pl.errores:
		return pl
	resueltos = _tipos(pl)
	_habitaciones(pl, resueltos)
	_temporadas(pl, resueltos)
	return pl


def _hora(v) -> str | None:
	"""«14:00:00» de un Time de Frappe (str «14:00:00» o «9:00:00», timedelta o time)."""
	if v in (None, ""):
		return None
	if isinstance(v, datetime.timedelta):
		s = int(v.total_seconds())
		return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"
	if isinstance(v, datetime.time):
		return v.strftime("%H:%M:%S")
	partes = [int(x) for x in str(v).split(".")[0].split(":")] + [0, 0]
	return f"{partes[0]:02d}:{partes[1]:02d}:{partes[2]:02d}"


def _condiciones(pl: Plan) -> dict:
	c = pl.planilla.condiciones
	deseado = {}
	if c.retencion_minutos:
		deseado["hold_minutes"] = c.retencion_minutos
	elif not pl.estado.propiedad.get("hold_minutes"):
		deseado["hold_minutes"] = RETENCION_MINUTOS
	for campo, valor in (
		("checkin_time", c.entrada),
		("checkout_time", c.salida),
		("free_cancel_days", c.cancelacion_gratis_dias),
		("cancellation_fee", c.cargo_cancelacion),
		("no_show_charge", c.cargo_no_show),
		("pets_allowed", None if c.mascotas is None else int(c.mascotas)),
	):
		if valor is not None:
			deseado[campo] = valor
	if pl.estado.tramos_cancelacion and c.cancelacion_gratis_dias is not None:
		n = pl.estado.tramos_cancelacion
		pl.avisos.append(
			f"«{pl.propiedad}» tiene {n} tramos de cancelación en Kamra: mandan sobre los {c.cancelacion_gratis_dias} días "
			"de la planilla; quítalos en Kamra (la carga no los toca)."
		)
	return deseado


def _propiedad(pl: Plan) -> None:
	actual = dict(pl.estado.propiedad)
	for campo in ("checkin_time", "checkout_time"):
		actual[campo] = _hora(actual.get(campo))
	deseado = {"country": PAIS, "booking_payment_mode": MODO_DE_COBRO, "deposit_pct": DEPOSITO_PCT, **_condiciones(pl)}
	if actual.get("timezone") in ZONAS_POR_DEFECTO:
		deseado["timezone"] = HOTEL_TIMEZONE
	moneda = pl.planilla.moneda
	if actual.get("currency") != moneda:
		if pl.estado.reservas:
			pl.errores.append(
				f"«{pl.propiedad}» cobra en {actual.get('currency') or '(sin moneda)'} y la planilla trae precios en "
				f"{moneda}; ya tiene {pl.estado.reservas} reservas, así que la carga no cambia la moneda: decídelo en "
				"Kamra."
			)
			return
		deseado["currency"] = moneda
	valores, antes = _diferencias(actual, deseado)
	pl.acciones.append(
		Accion("Property", "cambiar" if valores else "igual", valores, actual.get("name"), antes)
	)


def _tipos(pl: Plan) -> dict[str, str | None]:
	"""Crea o cambia los tipos; devuelve {tipo de la planilla: nombre en Kamra, o None si se crea}."""
	por_nombre = {_clave(t.get("room_type_name")): t for t in pl.estado.tipos}
	usados = {(t.get("room_type_code") or "").casefold() for t in pl.estado.tipos}
	resueltos: dict[str, str | None] = {}
	for t in pl.planilla.tipos.values():
		if not t.habitaciones:
			continue
		deseado = {
			"room_type_name": t.nombre,
			"base_price": t.precio_minimo,
			"adults_capacity": t.adultos,
			"base_occupancy": t.adultos,
			"children_capacity": t.ninos if t.ninos is not None else max(t.adultos - 1, 0),
			"max_total_occupants": t.adultos,
		}
		if pl.planilla.condiciones.por_habitacion:
			deseado.update(children_within_base_occupancy=1, child_price=0, extra_adult_price=0)
		actual = por_nombre.get(_clave(t.nombre))
		if actual is None:
			code = codigo(t.nombre, usados)
			usados.add(code.casefold())
			pl.acciones.append(Accion("Room Type", "crear", {**deseado, "room_type_code": code}, tipo=t.nombre))
			resueltos[t.nombre] = None
			continue
		valores, antes = _diferencias(actual, deseado)
		pl.acciones.append(
			Accion("Room Type", "cambiar" if valores else "igual", valores, actual["name"], antes, t.nombre)
		)
		resueltos[t.nombre] = actual["name"]
	sobran = sorted(
		t.get("room_type_name") or t["name"]
		for k, t in por_nombre.items()
		if k not in {_clave(n) for n in pl.planilla.tipos}
	)
	if sobran:
		que = "un tipo" if len(sobran) == 1 else f"{len(sobran)} tipos"
		pl.avisos.append(f"En Kamra hay {que} que la planilla no trae: {', '.join(sobran)} (no se toca).")
	return resueltos


def _notas(camas: str | None, notas: str | None) -> str | None:
	return " · ".join(x for x in (camas, notas) if x) or None


def _habitaciones(pl: Plan, resueltos: dict) -> None:
	por_numero = {str(h.get("room_number")): h for h in pl.estado.habitaciones}
	for h in pl.planilla.habitaciones:
		deseado = {"room_number": h.numero, "room_type": h.tipo, "floor": h.piso, "notes": _notas(h.camas, h.notas)}
		actual = por_numero.get(h.numero)
		if actual is None:
			pl.acciones.append(Accion("Room", "crear", deseado, tipo=h.tipo))
			continue
		comparable = {**deseado, "room_type": resueltos.get(h.tipo) or f"(nuevo) {h.tipo}"}
		valores, antes = _diferencias(actual, comparable)
		if "room_type" in valores:
			valores["room_type"] = h.tipo
		pl.acciones.append(
			Accion("Room", "cambiar" if valores else "igual", valores, actual["name"], antes, h.tipo)
		)
	numeros = {h.numero for h in pl.planilla.habitaciones}
	sobran = sorted(n for n in por_numero if n not in numeros)
	if sobran:
		que = "1 habitación" if len(sobran) == 1 else f"{len(sobran)} habitaciones"
		pl.avisos.append(f"En Kamra hay {que} que la planilla no trae: {', '.join(sobran)} (no se toca).")


def _temporadas(pl: Plan, resueltos: dict) -> None:
	nuestros = {v for v in resueltos.values() if v}
	nombres = {t.nombre for t in pl.planilla.temporadas} | set(pl.planilla.sin_fechas)
	existentes = {}
	for t in pl.estado.temporadas:
		clave = (t.get("room_type"), t.get("season_name"), str(t.get("start_date")), str(t.get("end_date")))
		existentes.setdefault(clave, t)
	usadas = set()
	for s in pl.planilla.temporadas:
		deseado = {
			"season_name": s.nombre,
			"room_type": s.tipo,
			"start_date": s.desde,
			"end_date": s.hasta,
			"adjustment_type": "Absolute",
			"adjustment_value": s.precio,
			"minimum_nights": s.minimo,
			"advance_percent": 0,
			"full_payment": 0,
			"disabled": 0,
		}
		rt = resueltos.get(s.tipo)
		actual = existentes.get((rt, s.nombre, str(s.desde), str(s.hasta))) if rt else None
		if actual is None:
			pl.acciones.append(Accion("Season", "crear", deseado, tipo=s.tipo))
			continue
		usadas.add(actual["name"])
		gestionados = {k: deseado[k] for k in ("adjustment_type", "adjustment_value", "minimum_nights",
		                                        "advance_percent", "disabled")}
		valores, antes = _diferencias(actual, gestionados)
		if valores:
			valores = {"season_name": s.nombre, "room_type": s.tipo, "start_date": s.desde, "end_date": s.hasta,
			           **valores}
		pl.acciones.append(
			Accion("Season", "cambiar" if valores else "igual", valores, actual["name"], antes, s.tipo)
		)
	for t in pl.estado.temporadas:
		if t["name"] in usadas or t.get("disabled"):
			continue
		if t.get("room_type") in nuestros and t.get("season_name") in nombres:
			pl.acciones.append(
				Accion("Season", "retirar", {"disabled": 1}, t["name"], {"disabled": 0}, _tipo_de(t, resueltos))
			)
		elif float(t.get("advance_percent") or 0):
			pl.avisos.append(
				f"La temporada «{t.get('season_name')}» ({t['name']}) pide un anticipo del "
				f"{float(t['advance_percent']):g} % del {_dd(t.get('start_date'))} al {_dd(t.get('end_date'))}: en "
				"esas fechas manda sobre «Full online» y el huésped no pagaría el total; quítaselo en Kamra (la carga no "
				"toca lo que no es suyo)."
			)


def _tipo_de(t: dict, resueltos: dict) -> str | None:
	for tipo, nombre in resueltos.items():
		if nombre == t.get("room_type"):
			return tipo
	return None


def _dd(d) -> str:
	if isinstance(d, datetime.date):
		return d.strftime("%d-%m-%Y")
	try:
		return datetime.date.fromisoformat(str(d)).strftime("%d-%m-%Y")
	except ValueError:
		return str(d)


def _num(v) -> str:
	return f"{float(v):g}" if isinstance(v, int | float) else str(v)


_VERBOS = {
	False: {"crear": "crearía", "cambiar": "cambiaría", "igual": "igual", "retirar": "retiraría"},
	True: {"crear": "creados", "cambiar": "cambiados", "igual": "igual", "retirar": "retirados"},
}
_TITULOS = {"Property": "Propiedad", "Room Type": "Tipos", "Room": "Habitaciones", "Season": "Temporadas"}
_SIGNOS = {"crear": "+", "cambiar": "~", "retirar": "-"}


def _linea(pl: Plan, a: Accion) -> list[str]:
	v, moneda = a.valores, pl.planilla.moneda
	signo = _SIGNOS[a.op]
	if a.doctype == "Property":
		return [f"  {k}: {a.antes.get(k) if a.antes.get(k) not in (None, '') else '(vacío)'} → {_num(x)}"
		        for k, x in v.items()]
	if a.doctype == "Room Type":
		if a.op == "crear":
			return [f"  + {v['room_type_name']} ({v['room_type_code']}): precio base {_num(v['base_price'])} {moneda}, "
			        f"{v['max_total_occupants']} personas (niños hasta {v['children_capacity']})"]
		return [f"  ~ {a.tipo}: " + ", ".join(f"{k} {_num(a.antes.get(k))} → {_num(x)}" for k, x in v.items())]
	if a.doctype == "Room":
		if a.op == "crear":
			piso = f", piso {v['floor']}" if v.get("floor") else ""
			return [f"  + {v['room_number']}: {v['room_type']}{piso}"]
		return [f"  ~ {a.nombre}: " + ", ".join(f"{k} {a.antes.get(k) or '(vacío)'} → {x}" for k, x in v.items())]
	if a.op == "retirar":
		return [f"  - {a.nombre} (se desactiva: la planilla ya no la trae con esas fechas)"]
	minimo = v["minimum_nights"] if a.op == "crear" else v.get("minimum_nights")
	cabeza = f"  {signo} {v['season_name']}, {_dd(v['start_date'])} → {_dd(v['end_date'])}, {v['room_type']}"
	if a.op == "crear":
		noches = "noche" if minimo == 1 else "noches"
		return [f"{cabeza}: {_num(v['adjustment_value'])} {moneda}/noche, mínimo {minimo} {noches}"]
	cambios = {k: x for k, x in v.items() if k not in ("season_name", "room_type", "start_date", "end_date")}
	return [f"{cabeza}: " + ", ".join(f"{k} {_num(a.antes.get(k))} → {_num(x)}" for k, x in cambios.items())]


def _texto(pl: Plan, aplicado: bool) -> str:
	verbos = _VERBOS[aplicado]
	cabeza = "APLICADA." if aplicado else "PRUEBA: no se ha escrito nada."
	out = [f"Carga de la planilla en «{pl.propiedad}» — {cabeza}"]
	r = pl.planilla.recuento()
	tipos = ", ".join(f"{k} {n}" for k, n in r["por_tipo"].items())
	out.append(
		f"Planilla: {r['habitaciones']} habitaciones ({tipos}) · {r['temporadas']} temporadas con fechas "
		f"({r['filas_de_precio']} precios) · {pl.planilla.moneda or 'moneda ?'}"
	)
	recuento = pl.recuento()
	for doctype, titulo in _TITULOS.items():
		if doctype not in recuento:
			continue
		cuenta = " · ".join(f"{verbos[op]} {recuento[doctype][op]}" for op in OPS if op in recuento[doctype])
		out.append(f"{titulo}: {cuenta}")
		for a in pl.acciones:
			if a.doctype == doctype and a.op != "igual":
				out.extend(_linea(pl, a))
	if pl.avisos:
		out.append(f"Avisos ({len(pl.avisos)}):")
		out.extend(f"  · {t}" for t in pl.avisos)
	if pl.errores:
		out.append(f"Errores ({len(pl.errores)}):")
		out.extend(f"  ✗ {t}" for t in pl.errores)
		que = "1 error" if len(pl.errores) == 1 else f"{len(pl.errores)} errores"
		out.append(f"RESULTADO: NO SE PUEDE CARGAR: {que} en la planilla o en Kamra (arriba).")
		return "\n".join(out)
	tot = {op: sum(1 for a in pl.acciones if a.op == op) for op in OPS}
	cuenta = ", ".join(f"{verbos[op]} {tot[op]}" for op in OPS)
	out.append(f"RESULTADO: {'cargada' if aplicado else 'lista para cargar'} ({cuenta}).")
	return "\n".join(out)
