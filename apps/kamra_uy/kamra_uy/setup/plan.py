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

HR-232 (D-HR-64, el fundador, 23-11: «Ponlo en 0, no pedimos garantía»): la propiedad sin depósito de garantía
(`security_deposit_amount` = 0), pero solo si sigue con el de fábrica de Kamra (5000, property.json), como la zona
horaria: uno puesto a mano después no se pisa, se avisa. Las propiedades ya cargadas lo reciben del patch
`kamra_uy.patches.v1_0.garantia_a_cero`, con la misma regla.

Las reservas ya tomadas (solo si se lee su pestaña): la clave de una de plataforma es su `ota_ref` (el código de la
plataforma, «código-n» por habitación si trae varias), la misma que usa Channex, así que una revisión posterior de
Channex la modifica o la cancela y no la duplica; la de una directa, su `idempotency_key` («planilla:<código>»). Se crean
y no se cambian nunca: desde que está en Kamra, la reserva la llevan Kamra y Channex, y una planilla vieja no la pisa
(una diferencia se avisa). Una que ya terminó no se carga; una en curso es un error (el check-in, en Kamra). Antes de
escribir, la prueba cuenta por tipo y por noche lo que ya ocupa Kamra más lo nuevo frente a las habitaciones del tipo:
si no caben, es un error y no se escribe nada.
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
GARANTIA = 0  # D-HR-64: el hotel no pide depósito de garantía
GARANTIA_DE_FABRICA = 5000  # la que Kamra pone en una propiedad nueva (Property.security_deposit_amount)

OPS = ("crear", "cambiar", "igual", "retirar")
CLAVE_DIRECTA = "planilla:"  # Reservation.idempotency_key de una reserva directa cargada de la planilla
VIVAS = ("Confirmed", "Checked In", "Held", "Pending Payment")  # las que ocupan habitación (kamra.reservation_state)


@dataclasses.dataclass
class Estado:
	"""Lo que hay en Kamra para la propiedad, en diccionarios con los nombres de campo de sus doctypes."""

	propiedad: dict | None
	reservas: int
	tipos: list
	habitaciones: list
	temporadas: list
	tramos_cancelacion: int = 0  # filas de Property.cancellation_tiers: si las hay, mandan sobre free_cancel_days
	# solo si se leen las reservas: las de Kamra con ota_ref o con clave de la carga (de cualquier estado), y las vivas
	# (VIVAS) que salen después de hoy, para contar la ocupación
	reservas_con_clave: list = dataclasses.field(default_factory=list)
	ocupadas: list = dataclasses.field(default_factory=list)


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


def garantia_de_fabrica(valor) -> bool:
	"""El depósito de garantía sigue como lo trae Kamra de fábrica: la carga y el patch lo ponen a 0 (D-HR-64)."""
	return valor is not None and _igual(valor, GARANTIA_DE_FABRICA)


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


def planea(planilla: Planilla, estado: Estado, propiedad: str | None = None, hoy: datetime.date | None = None) -> Plan:
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
	if planilla.con_reservas:
		_reservas(pl, resueltos, hoy or datetime.date.today())
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
	if garantia_de_fabrica(actual.get("security_deposit_amount")):
		deseado["security_deposit_amount"] = GARANTIA
	elif not _igual(actual.get("security_deposit_amount"), GARANTIA):
		pl.avisos.append(
			f"«{pl.propiedad}» pide en Kamra un depósito de garantía de {_num(actual['security_deposit_amount'])}, puesto "
			"a mano: la carga no lo toca (el hotel no pide garantía, D-HR-64)."
		)
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


def clave_de_reserva(r) -> tuple[str, str]:
	"""(campo de Reservation, valor) por el que Kamra reconoce una reserva de la planilla."""
	return ("ota_ref", r.ref) if r.ota else ("idempotency_key", CLAVE_DIRECTA + r.ref)


def _dia(v) -> datetime.date:
	return v if isinstance(v, datetime.date) else datetime.date.fromisoformat(str(v)[:10])


def _reservas(pl: Plan, resueltos: dict, hoy: datetime.date) -> None:
	en_kamra: dict[tuple[str, str], list] = {}
	for k in pl.estado.reservas_con_clave:
		if (k.get("ota_ref") or "").strip():
			en_kamra.setdefault(("ota_ref", k["ota_ref"].strip()), []).append(k)
		if (k.get("idempotency_key") or "").startswith(CLAVE_DIRECTA):
			en_kamra.setdefault(("idempotency_key", k["idempotency_key"]), []).append(k)
	de_la_planilla = {clave_de_reserva(r) for r in pl.planilla.reservas}
	usadas, terminadas, nuevas = set(), [], []
	for r in pl.planilla.reservas:
		donde = f"La reserva {r.ref} ({r.plataforma}, fila {r.fila})"
		campo, valor = clave_de_reserva(r)
		filas = en_kamra.get((campo, valor), [])
		actual = next((k for k in filas if k.get("status") != "Cancelled"), filas[0] if filas else None)
		if actual is not None:  # ya cargada (o entrada por Channex): se queda como está, pase lo que pase con sus fechas
			usadas.add(actual["name"])
			_compara_reserva(pl, r, actual, resueltos, donde)
			pl.acciones.append(Accion("Reservation", "igual", {}, actual["name"], tipo=r.tipo))
			continue
		if r.salida <= hoy:
			terminadas.append(r.ref)
			continue
		if r.llegada < hoy:
			pl.errores.append(
				f"{donde} empezó el {_dd(r.llegada)}: el huésped ya está en el hotel. Cárgala a mano en Kamra, con su "
				"check-in, y quítala de la planilla."
			)
			continue
		otra = _otra_forma(en_kamra, de_la_planilla, campo, ("" if r.ota else CLAVE_DIRECTA) + r.codigo)
		if otra:
			pl.errores.append(
				f"{donde}: en Kamra ya está como {otra[1]} ({otra[0]}), con otro número de habitaciones. Corrige la "
				"planilla o la reserva de Kamra para que digan lo mismo; la carga no la duplica."
			)
			continue
		nuevas.append(r)
		pl.acciones.append(Accion("Reservation", "crear", {
			"ref": r.ref,
			"plataforma": r.plataforma,
			"fila": r.fila,
			"room_type": r.tipo,
			"habitacion": r.habitacion,
			"check_in_date": r.llegada,
			"check_out_date": r.salida,
			"adults": r.adultos,
			"children": r.ninos,
			"source": r.origen,
			"channel": r.plataforma if r.ota else None,
			"ota_ref": r.ref if r.ota else None,
			"idempotency_key": None if r.ota else CLAVE_DIRECTA + r.ref,
			"total": r.total,
			"huesped": r.huesped,
			"telefono": r.telefono,
			"correo": r.correo,
			"notas": r.notas,
		}, tipo=r.tipo))
	if terminadas:
		que = "1 reserva ya terminó" if len(terminadas) == 1 else f"{len(terminadas)} reservas ya terminaron"
		pl.avisos.append(f"{que} (antes del {_dd(hoy)}) y no se carga{'' if len(terminadas) == 1 else 'n'}: "
		                 f"{', '.join(terminadas)}.")
	sobran = sorted(
		k["name"] for k in pl.estado.reservas_con_clave
		if k["name"] not in usadas and k.get("status") in VIVAS and _dia(k["check_out_date"]) > hoy
	)
	if sobran:
		que = "1 reserva" if len(sobran) == 1 else f"{len(sobran)} reservas"
		pl.avisos.append(f"En Kamra hay {que} de plataforma o de la carga que la planilla no trae: {', '.join(sobran)} "
		                 "(no se tocan).")
	for codigo, plataforma, ota in pl.planilla.canceladas:
		campo, base = ("ota_ref", codigo) if ota else ("idempotency_key", CLAVE_DIRECTA + codigo)
		viva = _otra_forma(en_kamra, set(), campo, base)
		if viva:
			pl.avisos.append(
				f"La reserva {codigo} ({plataforma}) está cancelada en la planilla y viva en Kamra ({viva[0]}): cancélala "
				"en Kamra si es así (la carga no cancela)."
			)
	_cabe(pl, resueltos, nuevas, hoy)


def _compara_reserva(pl: Plan, r, actual: dict, resueltos: dict, donde: str) -> None:
	"""Una reserva ya cargada no se toca: si Kamra la tiene distinta, o cancelada, se avisa."""
	if actual.get("status") == "Cancelled":
		pl.avisos.append(f"{donde} está cancelada en Kamra ({actual['name']}): la carga no la reabre.")
		return
	distinto = [
		nombre for nombre, a, b in (
			("el tipo", actual.get("room_type"), resueltos.get(r.tipo)),
			("la llegada", str(actual.get("check_in_date")), str(r.llegada)),
			("la salida", str(actual.get("check_out_date")), str(r.salida)),
			("los adultos", int(actual.get("adults") or 0), r.adultos),
			("los niños", int(actual.get("children") or 0), r.ninos),
		) if a != b
	]
	if distinto:
		lista = ", ".join(distinto[:-1]) + f" y {distinto[-1]}" if len(distinto) > 1 else distinto[0]
		pl.avisos.append(
			f"{donde} ya está en Kamra ({actual['name']}) con otr{'os datos' if len(distinto) > 1 else 'o dato'}: "
			f"{lista}. La carga no la toca: desde que está en Kamra, la llevan Kamra y Channex."
		)


def _otra_forma(en_kamra: dict, de_la_planilla: set, campo: str, base: str) -> tuple[str, str] | None:
	"""(nombre en Kamra, su clave) de una reserva viva de la misma reserva de la planilla guardada con otro número de
	habitaciones («X» frente a «X-0», «X-1»), que la planilla no trae con esa clave."""
	for (c, valor), filas in en_kamra.items():
		if c != campo or (c, valor) in de_la_planilla:
			continue
		if valor == base or (valor.startswith(base + "-") and valor[len(base) + 1:].isdigit()):
			viva = next((k for k in filas if k.get("status") != "Cancelled"), None)
			if viva:
				return viva["name"], valor.removeprefix(CLAVE_DIRECTA)
	return None


def _cabe(pl: Plan, resueltos: dict, nuevas: list, hoy: datetime.date) -> None:
	"""Por tipo y noche, lo que ya ocupa Kamra más las reservas nuevas no puede pasar de las habitaciones del tipo; y una
	habitación con número no puede tener dos reservas la misma noche."""
	if not nuevas:
		return
	tipo_de = {v: k for k, v in resueltos.items() if v}
	numeros = {h.numero for h in pl.planilla.habitaciones}
	numero_de = {h["name"]: str(h.get("room_number")) for h in pl.estado.habitaciones}
	cupo: dict[str, int] = {}
	for h in pl.planilla.habitaciones:
		cupo[h.tipo] = cupo.get(h.tipo, 0) + 1
	for h in pl.estado.habitaciones:
		t = tipo_de.get(h.get("room_type"))
		if t and str(h.get("room_number")) not in numeros:
			cupo[t] = cupo.get(t, 0) + 1
	por_tipo: dict[tuple[str, datetime.date], list] = {}
	por_habitacion: dict[tuple[str, datetime.date], list] = {}

	def suma(destino: dict, clave: str, desde, hasta, quien: str | None) -> None:
		d = max(_dia(desde), hoy)
		while d < _dia(hasta):
			destino.setdefault((clave, d), []).append(quien)
			d += datetime.timedelta(days=1)

	for o in pl.estado.ocupadas:
		t = tipo_de.get(o.get("room_type"))
		if t:
			suma(por_tipo, t, o["check_in_date"], o["check_out_date"], None)
		if o.get("room") in numero_de:
			suma(por_habitacion, numero_de[o["room"]], o["check_in_date"], o["check_out_date"], o["name"])
	for r in nuevas:
		suma(por_tipo, r.tipo, r.llegada, r.salida, r.ref)
		if r.habitacion:
			suma(por_habitacion, r.habitacion, r.llegada, r.salida, r.ref)
	refs = {r.ref for r in nuevas}
	vistos = set()
	for (tipo, d), quienes in sorted(por_tipo.items(), key=lambda x: (x[0][1], x[0][0])):
		hay = cupo.get(tipo, 0)
		if tipo in vistos or len(quienes) <= hay:
			continue
		vistos.add(tipo)
		nuevas_ahi = [q for q in quienes if q]
		ya = len(quienes) - len(nuevas_ahi)
		pl.errores.append(
			f"La noche del {_dd(d)} no caben las reservas de {tipo}: hay {hay} habitaciones y {len(quienes)} reservas "
			f"({ya} ya en Kamra y {len(nuevas_ahi)} de la planilla: {', '.join(nuevas_ahi)}). Corrígelo antes de cargar."
		)
	repetidas = set()
	for (numero, d), quienes in sorted(por_habitacion.items(), key=lambda x: (x[0][1], x[0][0])):
		if numero in repetidas or len(quienes) < 2 or not refs & set(quienes):
			continue
		repetidas.add(numero)
		pl.errores.append(
			f"La habitación {numero} tiene {len(quienes)} reservas la noche del {_dd(d)}: {', '.join(quienes)}. "
			"Deja en blanco la habitación de una (se asigna después en Kamra) o corrígelo antes de cargar."
		)


def _dd(d) -> str:
	if isinstance(d, datetime.date):
		return d.strftime("%d-%m-%Y")
	try:
		return datetime.date.fromisoformat(str(d)).strftime("%d-%m-%Y")
	except ValueError:
		return str(d)


def _personas(n: int, quien: str) -> str:
	return f"{n} {quien}{'' if n == 1 else 's'}"


def _num(v) -> str:
	return f"{float(v):g}" if isinstance(v, int | float) else str(v)


_VERBOS = {
	False: {"crear": "crearía", "cambiar": "cambiaría", "igual": "igual", "retirar": "retiraría"},
	True: {"crear": "creados", "cambiar": "cambiados", "igual": "igual", "retirar": "retirados"},
}
_TITULOS = {
	"Property": "Propiedad", "Room Type": "Tipos", "Room": "Habitaciones", "Season": "Temporadas",
	"Reservation": "Reservas",
}
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
	if a.doctype == "Reservation":  # solo se crean; ni el nombre ni el contacto del huésped salen en el informe
		hab = f", habitación {v['habitacion']}" if v.get("habitacion") else ""
		gente = _personas(v["adults"], "adulto") + (f" y {_personas(v['children'], 'niño')}" if v.get("children") else "")
		precio = f"total {_num(v['total'])} {moneda}" if v.get("total") else "al precio de Kamra"
		return [f"  + {v['ref']} ({v['plataforma']}): {v['room_type']}{hab}, {_dd(v['check_in_date'])} → "
		        f"{_dd(v['check_out_date'])}, {gente}, {precio}"]
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
	if pl.planilla.con_reservas and "reservas" in r:
		linea = f"Reservas en la planilla: {r['reservas']}"
		if r["reservas_por_plataforma"]:
			linea += " (" + ", ".join(f"{k} {n}" for k, n in r["reservas_por_plataforma"].items()) + ")"
		if r["reservas_canceladas"]:
			n = r["reservas_canceladas"]
			linea += " · 1 cancelada (no se carga)" if n == 1 else f" · {n} canceladas (no se cargan)"
		out.append(linea)
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
