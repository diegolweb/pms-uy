"""Las plataformas tampoco venden antes de la apertura (HR-264, D-HR-39): el cierre de venta en Channex hasta la víspera.

HR-263 cerró la reserva en línea antes de la apertura (kamra_uy.booking.opening), pero el ARI que Kamra manda a Channex
no sabe de `uy_opening_date`: con la venta abierta el 20-12 y la apertura el 26-12, Booking.com, Expedia o Despegar
venderían las noches del 20 al 25. Aquí la fecha de apertura cierra la venta en las plataformas con lo que Kamra ya
tiene para eso, una Channel Restriction (HR-17), como DATO y sin tocar Kamra:

- Con la apertura en el futuro, la propiedad tiene UNA Channel Restriction de kamra_uy: stop sell de hoy a la víspera
  de la apertura (kamra_uy.pure.opening.cierre), sin tipo de habitación ni plan (todos), todos los días de la semana y
  ninguna otra regla. Sin fecha, o con la apertura hoy o ya pasada, no hay ninguna. «Hoy» es el más temprano del reloj
  del sitio y el del hotel (kamra.no_show.property_now): ninguna noche que alguno de los dos aún vende queda abierta.
- `sync` la crea, la ajusta o la retira. La llaman el guardado de la Property (on_update, también al crearla), su
  renombre (after_rename) y su borrado (on_trash: la retira antes de que Frappe mire los enlaces), y cada hora el
  programador (`sync_all`, cron «5 * * * *» de hooks.py): corre la primera noche cuando cambia el día y retira la fila
  cuando llega la apertura, sin que nadie guarde la propiedad.
- Al crearla, cambiarla o borrarla, los doc_events de Kamra (kamra.channel_ari.on_ari_source_change) encolan sus fechas
  en el outbox de ARI de cada conexión Channex de la propiedad, y su trabajador las manda en la llamada `restrictions`
  (stop_sell en cada plan mapeado; al retirarla, stop_sell false en esas noches). Es la misma costura que la pantalla de
  restricciones: nada que reimplementar aquí.
- La fila es de kamra_uy por su nombre (kamra_uy.pure.opening.nombre_cierre: un prefijo propio y determinista), así que
  la clave primaria asegura que hay una sola. Las de la pantalla nacen con el nombre al azar de Kamra y nunca se tocan.
  Otra fila con el prefijo en la misma propiedad (la de antes de renombrarla) se retira.
- A mano, la fila de kamra_uy no se edita ni se borra (`protect`, validate y on_trash de Channel Restriction): se cambia
  o se vacía la fecha de apertura. Así nadie reabre las plataformas sin querer, ni nada lo deshace en silencio después.

La recepción no se frena: Kamra solo le avisa al guardar una reserva en noches cerradas (y una reserva de plataforma ya
vendida entra con una nota). La reserva en línea ya la rechaza antes el guardián de HR-263.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date

import frappe
from frappe.utils import cint, getdate, nowdate

from kamra_uy.booking import opening
from kamra_uy.pure import opening as regla

DOCTYPE = "Channel Restriction"
FLAG = "kamra_uy_opening_stop_sell"  # frappe.flags: kamra_uy escribe su propia fila (protect la deja pasar)
ERROR_TITLE = "kamra_uy: cierre de venta hasta la apertura"
_DATES = ("date_from", "date_to")
_NUMBERS = ("stop_sell", "closed_to_arrival", "closed_to_departure", "min_stay_arrival", "min_stay_through",
            "max_stay", "disabled")
_SETUP_FLAGS = ("in_install", "in_migrate", "in_patch")
_FROM_DB = object()


class ManagedRestrictionError(frappe.ValidationError):
	"""La Channel Restriction de la apertura no se edita ni se borra a mano: la mantiene la fecha de apertura."""


def restriction_name(property: str) -> str:
	"""El name de la fila de kamra_uy de esta propiedad."""
	return regla.nombre_cierre(property)


def today(property: str) -> date | None:
	"""Hoy para el cierre: el más temprano del día del sitio (el de las celdas de ARI) y el del hotel."""
	from kamra.no_show import property_now

	return regla.hoy(nowdate(), property_now(property))


def _own_rows(property: str) -> list[str]:
	"""Las filas de kamra_uy de la propiedad (por el prefijo del nombre)."""
	return frappe.get_all(DOCTYPE, filters={"property": property, "name": ("like", regla.PREFIJO_CIERRE + "%")},
	                      pluck="name", order_by="creation asc")


@contextmanager
def _own():
	"""Mientras dura, protect deja pasar la escritura: es kamra_uy quien la hace."""
	depth = cint(frappe.flags.get(FLAG))
	frappe.flags[FLAG] = depth + 1
	try:
		yield
	finally:
		frappe.flags[FLAG] = depth


def _desired(property: str, desde: date, hasta: date) -> dict:
	return {"property": property, "room_type": None, "rate_plan": None, "date_from": str(desde),
	        "date_to": str(hasta), "days_of_week": None, "stop_sell": 1, "closed_to_arrival": 0,
	        "closed_to_departure": 0, "min_stay_arrival": 0, "min_stay_through": 0, "max_stay": 0,
	        "note": regla.NOTA_CIERRE, "disabled": 0}


def _value(field: str, value):
	if field in _DATES:
		return str(getdate(value)) if value else ""
	if field in _NUMBERS:
		return cint(value)
	return value or ""


def _differs(doc, desired: dict) -> bool:
	return any(_value(f, doc.get(f)) != _value(f, v) for f, v in desired.items())


def _put(name: str, desired: dict, retry: bool = True) -> None:
	"""La fila con esos valores: la cambia solo si difiere (un guardado de más encolaría ARI de más)."""
	if frappe.db.exists(DOCTYPE, name):
		doc = frappe.get_doc(DOCTYPE, name)
		if _differs(doc, desired):
			doc.update(desired)
			doc.save(ignore_permissions=True)
		return
	try:
		frappe.get_doc({"doctype": DOCTYPE, **desired}).insert(ignore_permissions=True, set_name=name)
	except frappe.DuplicateEntryError:
		if not retry:
			raise
		_put(name, desired, retry=False)  # otro guardado la creó a la vez: se ajusta esa


def sync(property: str, apertura=_FROM_DB) -> str | None:
	"""Pone la fila de kamra_uy de la propiedad como dice su fecha de apertura (`apertura`; sin ella, la de la base): la
	crea, la ajusta o la retira. Devuelve su name si queda, None si no. Idempotente."""
	if not property:
		return None
	if apertura is _FROM_DB:
		apertura = opening.opening_date(property)
	rango = regla.cierre(today(property), apertura) if regla.fecha(apertura) else None
	name = restriction_name(property)
	with _own():
		for other in _own_rows(property):
			if not rango or other.upper() != name.upper():
				frappe.delete_doc(DOCTYPE, other, ignore_permissions=True)
		if not rango:
			return None
		_put(name, _desired(property, *rango))
	return name


def sync_all() -> None:
	"""Cada hora (hooks.py, cron «5 * * * *»): el cierre de cada propiedad con fecha de apertura o con una fila de
	kamra_uy. Una propiedad que falla va al Error Log y no frena a las demás."""
	properties = set(frappe.get_all(DOCTYPE, filters={"name": ("like", regla.PREFIJO_CIERRE + "%")},
	                                pluck="property"))
	if frappe.get_meta("Property").has_field(opening.FIELD):
		properties |= set(frappe.get_all("Property", filters={opening.FIELD: ("is", "set")}, pluck="name"))
	for property in sorted(p for p in properties if p):
		savepoint = "uy_cierre_" + frappe.generate_hash(length=8)
		frappe.db.savepoint(savepoint)
		try:
			sync(property)
		except Exception as exc:
			frappe.db.rollback(save_point=savepoint)
			frappe.log_error(title=ERROR_TITLE, message=f"{property} · {type(exc).__name__}: {exc}"[:1000])


def on_property_update(doc, method=None) -> None:
	"""on_update de Property (también al crearla): la fecha que se acaba de guardar."""
	sync(doc.name, doc.get(opening.FIELD))


def on_property_rename(doc, method=None, old=None, new=None, merge=False) -> None:
	"""after_rename de Property: Frappe movió la fila a la propiedad nueva con el nombre de la vieja; queda la buena."""
	sync(new or doc.name)


def on_property_trash(doc, method=None) -> None:
	"""on_trash de Property: sus filas de kamra_uy se retiran (si no, Frappe no deja borrarla por el enlace)."""
	with _own():
		for name in _own_rows(doc.name):
			frappe.delete_doc(DOCTYPE, name, ignore_permissions=True)


def protect(doc, method=None) -> None:
	"""validate y on_trash de Channel Restriction: la fila de kamra_uy solo la escribe kamra_uy. Las demás, libres."""
	if not regla.es_cierre(doc.name) or frappe.flags.get(FLAG):
		return
	if any(frappe.flags.get(f) for f in _SETUP_FLAGS):
		return
	frappe.throw(regla.CIERRE_BLOQUEADO, exc=ManagedRestrictionError)
