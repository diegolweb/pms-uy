"""No vender antes de la apertura (HR-263, D-HR-39): la fecha de apertura de la propiedad y su guardián de servidor.

El hotel abre después de abrir la venta (D-HR-39: la venta, el 20-12; la apertura, probablemente el 26-12, sin tarifa de
pretemporada). La propiedad lleva su fecha de apertura en `uy_opening_date`, un campo de kamra_uy (Date, vacío al nacer
= sin restricción) que crea `ensure_fields` en after_install y after_migrate, como los de HR-227: Kamra no se toca.

Con la fecha puesta, la reserva en línea no vende una estancia cuya LLEGADA sea anterior (kamra_uy.pure.opening):

- `search_stay` y `book` (kamra.public_api, las que llama la web y la página /book de Kamra) pasan por aquí antes que
  por Kamra (override_whitelisted_methods de hooks.py, que Frappe aplica en /api/method y en /api/v2/method): con una
  llegada anterior, rechazan con el motivo en el idioma de la página (`BeforeOpeningError`, un ValidationError) y no
  hacen nada más; con la del mismo día o posterior, son las de Kamra tal cual.
- `showcase` (la vitrina) añade `uy_opening_date` (AAAA-MM-DD o null): con ella la web acota la llegada mínima y
  pinta su línea informativa (web/src/booking).
- Y toda reserva Website que se vaya a crear por otro camino pasa por `on_reservation_before_insert` (before_insert de
  Reservation): la misma regla. La recepción no: puede alojar antes de abrir (las reservas de recepción no son Website).

El motivo es la línea de la web (`fechas.apertura` de reservar.md): un BORRADOR hasta el OK del fundador (D-HR-44).

Las plataformas (Channex) tampoco venden antes de la apertura: eso lo hace kamra_uy.booking.stop_sell (HR-264), con una
Channel Restriction stop sell de la propiedad de hoy a la víspera que el outbox de ARI de Kamra manda a Channex.
"""

from __future__ import annotations

import frappe

from kamra_uy.notifications import confirmation
from kamra_uy.pure import opening as regla

MODULE = "Kamra UY"
FIELD = "uy_opening_date"
SOURCE = "Website"
FIELDS = {
	"Property": [
		{"fieldname": FIELD, "label": "Fecha de apertura del hotel", "fieldtype": "Date",
		 "insert_after": "booking_engine_enabled", "module": MODULE,
		 "description": "La reserva en línea no vende estadías con llegada anterior a esta fecha, y la web lo dice en "
		                "una línea. Las plataformas (Channex) tampoco venden las noches anteriores: kamra_uy les cierra "
		                "la venta de hoy a la víspera. Vacía: sin restricción."},
	],
}


class BeforeOpeningError(frappe.ValidationError):
	"""La llegada es anterior a la apertura de la propiedad."""


def ensure_fields() -> None:
	"""Crea o pone al día el campo de la Property (after_install, after_migrate). Idempotente."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(FIELDS, update=True)


def opening_date(property: str | None):
	"""La fecha de apertura de la propiedad (date), o None si no la tiene o no existe."""
	if not property or not frappe.get_meta("Property").has_field(FIELD):
		return None
	return regla.fecha(frappe.db.get_value("Property", property, FIELD))


def _language(property: str) -> str:
	"""El idioma de la página que llama (`_lang` o el de la petición); sin él, el de la propiedad; sin ese, español."""
	return confirmation._page_language() or confirmation._codigo(
		frappe.db.get_value("Property", property, "booking_language")) or "es"


def guard(property: str | None, check_in_date) -> None:
	"""Rechaza una llegada anterior a la apertura de la propiedad (BeforeOpeningError)."""
	apertura = opening_date(property)
	if regla.antes_de_abrir(check_in_date, apertura):
		frappe.throw(regla.motivo(apertura, _language(property)), exc=BeforeOpeningError)


@frappe.whitelist(allow_guest=True)
def showcase(property: str, **kwargs):
	"""kamra.public_api.showcase con la fecha de apertura de la propiedad (`uy_opening_date`, AAAA-MM-DD o null)."""
	from kamra import public_api

	data = frappe.call(public_api.showcase, property=property, **kwargs)
	if isinstance(data, dict):
		apertura = opening_date(property)
		data[FIELD] = apertura.isoformat() if apertura else None
	return data


@frappe.whitelist(allow_guest=True)
def search_stay(property: str, check_in_date: str, **kwargs):
	"""kamra.public_api.search_stay, salvo una llegada anterior a la apertura: la rechaza."""
	from kamra import public_api

	guard(property, check_in_date)
	return frappe.call(public_api.search_stay, property=property, check_in_date=check_in_date, **kwargs)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def book(property: str, check_in_date: str, **kwargs):
	"""kamra.public_api.book, salvo una llegada anterior a la apertura: la rechaza antes de cotizar ni retener nada.
	El límite de book (rate_limit de Kamra) sigue contando en su función, con su mismo contador."""
	from kamra import public_api

	guard(property, check_in_date)
	return frappe.call(public_api.book, property=property, check_in_date=check_in_date, **kwargs)


def on_reservation_before_insert(doc, method=None) -> None:
	"""before_insert de Reservation: una reserva Website con llegada anterior a la apertura no se crea."""
	if doc.get("source") != SOURCE:
		return
	if any(frappe.flags.get(f) for f in ("in_import", "in_install", "in_migrate", "in_patch")):
		return
	guard(doc.get("property"), doc.get("check_in_date"))
