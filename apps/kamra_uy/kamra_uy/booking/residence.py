"""El 10 % de IVA del residente se cobra AL RESERVAR (HR-266, D-HR-47): la residencia declarada y el IVA de cada noche.

D-HR-47 (22-11, el fundador): «Al momento del check-in la factura ya debería estar pagada». La reserva en línea cobra el
total con el IVA que toque (D-HR-40): el 10 % en las noches del residente del 16 de noviembre al domingo de Pascua, y
todas las de un comprador residente con RUT; al extranjero, 0. Para saberlo al reservar, el huésped declara en el paso
de datos si reside en Uruguay (es la única forma: el documento se ve en el check-in).

- `book` (kamra.public_api.book, que pasa por aquí: override_whitelisted_methods de hooks.py) exige la declaración, `uy_resident` = «1» o «0», en una
  propiedad de Uruguay: sin ella rechaza (`ResidenceRequiredError`) antes de cotizar ni retener nada, así que una
  llamada directa sin la residencia no cobra un IVA equivocado. La web la manda (web/src/booking); la página /book de
  Kamra no la pregunta, así que en Uruguay no reserva (lo dice su motivo).
- La declaración viaja en `frappe.flags` hasta la reserva (kamra.authz.act_as vacía form_dict) y queda en su campo
  `uy_declared_residence` (before_insert, `remember_declaration`), que recepción ve.
- `on_reservation_validate` (antes que el de fiscal) pone en `night_rates` la tasa de cada noche que facturará core
  (`uy_tax.booking_room_rates`, la misma regla que services/core/config/iva-rules.uy.yaml) y corrige el total: Kamra
  calcula con él lo que se paga al reservar (`advance_due`), y de ahí salen el enlace de pago, la tarjeta y el saldo de
  Mercado Pago (kamra_uy.payments). Lo hace mientras la reserva no esté en casa: en el check-in manda core.
- Lo declarado es si el DOCUMENTO es uruguayo (D-HR-74: manda el país del documento, no dónde vive; HR-350 cambió la
  pregunta de la web y HR-346 la de recepción). El valor guardado no cambió («Reside en Uruguay» = documento uruguayo).
- En el check-in core decide con el documento (fiscal.connector.adjust_iva): recepción indica de qué país es (el paso
  «residence» de Kamra guarda `Guest.uy_document_country`; la cédula es uruguaya sin preguntarlo) y sin él no hay
  check-in. Si coincide con lo declarado, las tasas no cambian y no se cobra nada. Si lo contradice, `on_reservation_update` apunta en la cuenta del huésped la diferencia
  de IVA a cobrar o a devolver (un comentario; las noches se cargan de una en una con la auditoría nocturna, así que el
  saldo del folio aún no la enseña entera). La factura (CFE) sigue saliendo en el check-in.
- `booking_status` y `book` devuelven el desglose (`uy_iva`): sin IVA, IVA, total, la tasa y cuántas noches la llevan.

Con `Property.rates_include_tax` el precio configurado ya lleva el IVA (uruguay.calculate_room_tax lo saca a la tasa de
habitación): no se toca nada aquí.
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal

import frappe
from frappe.utils import flt, fmt_money, getdate

from kamra_uy.fiscal import connector, model
from kamra_uy.notifications import confirmation
from kamra_uy.pure import uy_tax

MODULE = "Kamra UY"
FIELD = "uy_declared_residence"
PARAM = "uy_resident"
FLAG = "kamra_uy_declared_residence"
SOURCE = "Website"
AT_HOME = ("Checked In", "Checked Out")

FIELDS = {
	"Reservation": [
		{"fieldname": FIELD, "label": "Documento declarado al reservar", "fieldtype": "Select",
		 "options": "\n".join(["", uy_tax.RESIDENT, uy_tax.NON_RESIDENT]), "no_copy": 1, "print_hide": 1,
		 "insert_after": "source", "module": MODULE,
		 "description": "Lo que el huésped declaró en la reserva en línea: «Reside en Uruguay» es documento uruguayo y «No "
		                "reside en Uruguay», documento extranjero. Con eso la reserva lleva desde que se crea el IVA de "
		                "cada noche (10 % con documento uruguayo del 16 de noviembre al domingo de Pascua; 0 con documento "
		                "extranjero). En el check-in manda el documento: si no coincide, se cobra o se devuelve la "
		                "diferencia."},
	],
}

# El motivo del rechazo, en el idioma de la página (aprobado con todos los textos el 22-11, D-HR-48). La web no lo enseña
# nunca (la pregunta es obligatoria en su formulario); sí una llamada sin ella.
MISSING = {
	"es": "Indica si tu documento es uruguayo: el IVA de tu reserva depende de eso.",
	"pt": "Informe se o seu documento é uruguaio: o IVA da sua reserva depende disso.",
	"en": "Tell us whether your ID document is Uruguayan: the VAT on your booking depends on it.",
}

# La nota de recepción en la cuenta del huésped (aprobada con D-HR-48): el escritorio va en español.
NOTE = ("IVA: al reservar, el huésped declaró «{declared}», pero el documento del check-in no coincide. "
        "Diferencia de IVA {word}: {amount}.")
# lo declarado, como lo lee recepción en la nota (el valor guardado no cambió con D-HR-74)
DECLARED_TEXT = {uy_tax.RESIDENT: "documento uruguayo", uy_tax.NON_RESIDENT: "documento extranjero"}
# HR-304 y HR-346: el check-in de una propiedad de Uruguay no se hace sin el país del documento (nunca se supone)
CHECK_IN_NEEDS = ("Antes del check-in, indique si el documento del huésped es uruguayo y, si no, de qué país es: de eso "
                  "depende el IVA de la factura.")
UNKNOWN_COUNTRY = "No se reconoce el país «{country}»: elíjalo de la lista."
AT_HOME_DECLARED = ("El huésped ya está en el hotel: indique el país del documento en el check-in, no en la "
                    "reserva.")
COLLECT_WORD = "a cobrar"
REFUND_WORD = "a devolver"


class ResidenceRequiredError(frappe.ValidationError):
	"""La reserva en línea de una propiedad de Uruguay sin la residencia declarada."""


def ensure_fields() -> None:
	"""Crea o pone al día el campo de la Reservation (after_install, after_migrate). Idempotente."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(FIELDS, update=True)


def applies(property: str | None) -> bool:
	"""Si la propiedad es de Uruguay (y existe): solo ahí se pregunta y se cobra el IVA al reservar."""
	return bool(property) and bool(frappe.db.exists("Property", property)) and connector.is_uruguay(property)


def _has_field() -> bool:
	return frappe.get_meta("Reservation").has_field(FIELD)


def require(property: str, value) -> str | None:
	"""Lo declarado (uy_tax.RESIDENT o NON_RESIDENT) si la propiedad es de Uruguay; si no hay, ResidenceRequiredError
	con el motivo en el idioma de la página. Fuera de Uruguay, None: no se pregunta."""
	if not applies(property):
		return None
	declared = uy_tax.declared_residence(value)
	if declared is None:
		frappe.throw(MISSING.get(_language(property), MISSING["es"]), exc=ResidenceRequiredError)
	return declared


def _language(property: str) -> str:
	"""El idioma de la página que llama (`_lang` o el de la petición); sin él, el de la propiedad; sin ese, español."""
	return confirmation._page_language() or confirmation._codigo(
		frappe.db.get_value("Property", property, "booking_language")) or "es"


@frappe.whitelist(allow_guest=True, methods=["POST"])
def book(property: str, uy_resident=None, **kwargs):
	"""kamra.public_api.book que, en Uruguay, exige la residencia declarada (`uy_resident`, «1» o «0»): sin ella rechaza
	antes de cotizar ni retener nada, y con ella la reserva nace con el IVA de cada noche y la respuesta lleva su
	desglose. Fuera de Uruguay es la de Kamra tal cual. El límite de book (rate_limit de Kamra) sigue contando en su
	función, con su mismo contador."""
	from kamra import public_api

	declared = require(property, uy_resident)
	with declaring(declared):
		data = frappe.call(public_api.book, property=property, **kwargs)
	return with_breakdown(data)


@contextmanager
def declaring(declared: str | None):
	"""La declaración, en frappe.flags mientras Kamra crea la reserva (act_as vacía form_dict)."""
	frappe.flags[FLAG] = declared
	try:
		yield
	finally:
		frappe.flags.pop(FLAG, None)


def remember_declaration(doc, method=None) -> None:
	"""before_insert de Reservation: lo que declaró el huésped en la reserva en línea."""
	declared = frappe.flags.get(FLAG)
	if declared and doc.get("source") == SOURCE and not doc.get(FIELD) and _has_field():
		doc.set(FIELD, declared)


def buyer_has_rut(res) -> bool:
	"""Si la reserva se factura a una empresa con un RUT válido (como fiscal.connector.guest_inputs)."""
	if not res.get("company"):
		return False
	gstin = frappe.db.get_value("Corporate Account", res.company, "gstin") or ""
	return model.valid_rut("".join(ch for ch in gstin if ch.isdigit()))


def _room_pct(res):
	"""La tasa de habitación de la propiedad (o la del tipo de habitación), como uruguay.calculate_room_tax."""
	rates = frappe.db.get_value("Property", res.property, ["uy_iva_room_pct", "rates_include_tax"], as_dict=True) or {}
	room_type_pct = frappe.db.get_value("Room Type", res.room_type, "tax_percent") if res.get("room_type") else None
	return uy_tax.room_rate(room_type_pct, rates.get("uy_iva_room_pct")), bool(rates.get("rates_include_tax"))


def apply(res) -> bool:
	"""El IVA de cada noche de lo declarado, en la reserva en memoria (la guarda quien llamó). True si cambió algo."""
	declared = res.get(FIELD)
	if declared not in (uy_tax.RESIDENT, uy_tax.NON_RESIDENT) or res.get("status") in AT_HOME:
		return False
	if not res.get("night_rates") or not applies(res.property):
		return False
	pct, included = _room_pct(res)
	if included:
		return False
	rates = uy_tax.booking_room_rates(declared, getdate(res.check_in_date), getdate(res.check_out_date),
	                                  buyer_has_rut=buyer_has_rut(res), room_pct=pct)
	return connector.apply_room_rates(res, rates)


def on_reservation_validate(doc, method=None) -> None:
	"""validate de Reservation (antes que fiscal.events): el IVA de lo declarado, desde que se crea."""
	if doc.get(FIELD):
		apply(doc)


def breakdown(res) -> dict | None:
	"""El desglose de lo que se cobra: sin IVA, IVA, total, la tasa y cuántas noches la llevan. None sin declaración."""
	declared = res.get(FIELD) if _has_field() else None
	if declared not in (uy_tax.RESIDENT, uy_tax.NON_RESIDENT):
		return None
	taxed = [flt(r.gst_rate) for r in res.get("night_rates") or [] if flt(r.gst_rate) > 0]
	return {
		"residence": "resident" if declared == uy_tax.RESIDENT else "non_resident",
		"amount_before_tax": round(flt(res.amount_before_tax), 2),
		"tax_amount": round(flt(res.tax_amount), 2),
		"amount_after_tax": round(flt(res.amount_after_tax), 2),
		"iva_pct": max(taxed) if taxed else 0.0,
		"iva_nights": len(taxed),
	}


@frappe.whitelist(allow_guest=True)
def booking_status(token: str) -> dict:
	"""kamra.public_api.booking_status con el desglose del IVA (`uy_iva`, null sin declaración) y el Room Type de la
	reserva (`room_type`, HR-273): la web casa por él la ficha de la habitación del resumen, no por su nombre."""
	from kamra import public_api

	data = frappe.call(public_api.booking_status, token=token)
	if isinstance(data, dict) and data.get("reservation"):
		res = frappe.get_doc("Reservation", data["reservation"])
		data["uy_iva"] = breakdown(res)
		data["room_type"] = res.room_type or None
	return data


def with_breakdown(data):
	"""La respuesta de book con su desglose (`uy_iva`)."""
	if isinstance(data, dict) and data.get("reservation"):
		data["uy_iva"] = breakdown(frappe.get_doc("Reservation", data["reservation"]))
	return data


# ── el check-in ─────────────────────────────────────────────────────────────────────────────────────────────────
def on_reservation_update(doc, method=None) -> None:
	"""on_update de Reservation: al entrar en casa, si core (con el documento) cambió el IVA de lo declarado, la
	diferencia a cobrar o a devolver, apuntada en la cuenta del huésped. Nunca para el check-in."""
	before = doc.get_doc_before_save()
	if doc.status != "Checked In" or (before and before.status == "Checked In") or not before:
		return
	if not doc.get(FIELD) or not applies(doc.property):
		return
	diff = Decimal(str(flt(doc.tax_amount))) - Decimal(str(flt(before.tax_amount)))
	if abs(diff) < uy_tax.CENT:
		return
	sp = "kamra_uy_residence"
	frappe.db.savepoint(sp)
	try:
		currency = frappe.get_cached_value("Property", doc.property, "currency")
		text = NOTE.format(declared=DECLARED_TEXT.get(doc.get(FIELD), doc.get(FIELD)),
		                   word=COLLECT_WORD if diff > 0 else REFUND_WORD,
		                   amount=fmt_money(float(abs(diff)), currency=currency))
		folio = connector._guest_folio(doc.name)
		frappe.get_doc({"doctype": "Comment", "comment_type": "Comment",
		                "reference_doctype": "Folio" if folio else "Reservation",
		                "reference_name": folio or doc.name, "content": text}).insert(ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point=sp)
		frappe.log_error(title="kamra_uy: diferencia de IVA en el check-in")


# ── recepción (HR-304, HR-346): lo que el paquete de Uruguay le da a Kamra (kamra.localization, «residence») ───────
# El paso de Kamra se llama «residence», pero en Uruguay lo que pregunta es el país del DOCUMENTO (D-HR-74): «home» es
# documento uruguayo y el país de «abroad», el que emitió el documento. Lo guarda Guest.uy_document_country; el país de
# residencia (Guest.residence_country) queda como dato.
def document_country(res) -> str | None:
	"""El país (ISO) del documento del huésped de la reserva (model.document_country), o None si no se sabe."""
	if not res.get("guest"):
		return None
	g = frappe.db.get_value("Guest", res.guest, ["id_type", "uy_document_country"], as_dict=True) or {}
	return model.document_country(g.get("id_type"), connector._iso(g.get("uy_document_country")))


def _country_name(code: str | None) -> str | None:
	return frappe.db.get_value("Country", {"code": code.lower()}, "name") if code else None


def desk_residence(reservation: str) -> dict | None:
	"""Lo que recepción ve en el paso del documento de una reserva de Uruguay (None fuera de Uruguay): lo declarado al
	reservar (`declared`, «resident» = documento uruguayo, «non_resident» = extranjero, o None), el país del documento
	(`country`, el nombre del país), si aún se puede declarar en la reserva (`can_declare`: antes del check-in) y si
	falta para facturar (`missing`: sin el país del documento; el check-in no se hace así, HR-346)."""
	res = frappe.db.get_value("Reservation", reservation, ["name", "property", "status", "guest", FIELD],
	                          as_dict=True) if _has_field() else None
	if not res or not applies(res.property):
		return None
	country = document_country(res)
	return {
		"home_country": connector.COUNTRY,
		"declared": {uy_tax.RESIDENT: "resident", uy_tax.NON_RESIDENT: "non_resident"}.get(res.get(FIELD)),
		"country": _country_name(country),
		"can_declare": res.status not in AT_HOME,
		"missing": country is None,
	}


def declare(reservation: str, resident: bool) -> None:
	"""Recepción declara la residencia de una reserva antes del check-in (la del teléfono o la de una plataforma): la
	reserva lleva desde ahí el IVA de cada noche (on_reservation_validate), y el enlace de pago, lo que falta con él."""
	doc = frappe.get_doc("Reservation", reservation)
	doc.check_permission("write")
	if not applies(doc.property):
		frappe.throw("Esta propiedad no pregunta la residencia.")
	if doc.status in AT_HOME:
		frappe.throw(AT_HOME_DECLARED)
	doc.set(FIELD, uy_tax.RESIDENT if resident else uy_tax.NON_RESIDENT)
	doc.save()


def on_new_booking(doc, resident: bool) -> None:
	"""La reserva de recepción que se va a crear (kamra.api.create_booking) con lo declarado por teléfono."""
	if _has_field() and applies(doc.get("property")):
		doc.set(FIELD, uy_tax.RESIDENT if resident else uy_tax.NON_RESIDENT)


def quote_for(quote: dict, resident: bool, inputs: dict) -> dict:
	"""La cotización de recepción (kamra.api.get_quote) con el IVA de cada noche de lo declarado, como la reserva que
	se va a crear con ella y como la reserva en línea: el 10 % del residente en alta temporada (D-HR-40, D-HR-47).
	Fuera de Uruguay, con precios con IVA incluido o sin noches, la de Kamra tal cual. Lleva `residence`."""
	property = inputs.get("property")
	if not isinstance(quote, dict) or not applies(property):
		return quote
	declared = uy_tax.RESIDENT if resident else uy_tax.NON_RESIDENT
	res = frappe._dict(property=property, room_type=inputs.get("room_type"), company=inputs.get("company"))
	pct, included = _room_pct(res)
	out = dict(quote)
	if not included and quote.get("nightly"):
		rates = uy_tax.booking_room_rates(declared, getdate(inputs["check_in_date"]), getdate(inputs["check_out_date"]),
		                                  buyer_has_rut=buyer_has_rut(res), room_pct=pct)
		out = uy_tax.quote_with_rates(quote, rates)
	out["residence"] = "resident" if resident else "non_resident"
	return out


def save_country(guest: str, country: str) -> None:
	"""El país del documento del huésped que recepción indica en el check-in (Guest.uy_document_country): el nombre del
	país o su código ISO; uno que no existe se rechaza. Cambiarlo con el huésped en casa reajusta el IVA de las noches
	que faltan y saca el comprobante que esperaba (fiscal.events.on_guest_update)."""
	value = (country or "").strip()
	name = frappe.db.exists("Country", value) or (
		frappe.db.get_value("Country", {"code": value.lower()}, "name") if len(value) == 2 else None)
	if not name:
		frappe.throw(UNKNOWN_COUNTRY.format(country=value))
	doc = frappe.get_doc("Guest", guest)
	if doc.get("uy_document_country") != name:
		doc.uy_document_country = name
		doc.save()


def require_at_check_in(doc, method=None) -> None:
	"""validate de Reservation (el primero): en Uruguay, el check-in no se hace sin el país del documento del huésped
	(`document_country`): la factura sale en el check-in y nunca supone de dónde es (HR-304, HR-346)."""
	if doc.get("status") != "Checked In" or not applies(doc.get("property")):
		return
	before = doc.get_doc_before_save()
	if before is not None and before.status == "Checked In":
		return
	if document_country(doc) is None:
		frappe.throw(CHECK_IN_NEEDS, exc=ResidenceRequiredError)
