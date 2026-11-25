"""El recibo del hotel al cobrar una reserva antes del check-in (HR-349, D-HR-74: «con la reserva corresponde recibo
siempre»; la factura, al prestar el servicio).

Todo cobro pasa por una línea nueva del folio (`Folio.payments`): el aviso `payment.approved` de core
(kamra.payments.settle_payment_link, también con la tarjeta de la web), el enlace de pago y recepción
(kamra.api.add_folio_payment, record_advance). `on_folio_update` (on_update de Folio, hooks.py) mira solo las líneas
que trae ese guardado y, para cada una que la lleva (kamra_uy.receipts.model.kind_for), emite un UY Receipt:

- numerado por Property y sin huecos: el número sale bloqueando la fila de la Property hasta el final de la
  transacción, y si el cobro se deshace, se deshace con él (la unicidad de `property`+`number` lo guarda también);
- uno por línea de pago (`payment_row` único): el mismo aviso dos veces no asienta otra línea, así que no hace otro;
- con el emisor (razón social y RUT de la Property), el pagador, la fecha, el importe y la moneda del enlace, lo cobrado
  en pesos si Mercado Pago cobró en pesos, y la reserva y su estadía;
- la devolución de un pago con recibo deja su constancia, en la misma serie y con el importe negativo.

Nunca frena el cobro: si no se puede emitir, se deshace solo el recibo y su porqué (el tipo del error) va al Error Log
del folio. El correo de confirmación lleva el enlace del recibo (kamra_uy.notifications.confirmation) y la reserva lo
enseña en sus conexiones (`reservation_dashboard`).
"""

from __future__ import annotations

from urllib.parse import urlencode

import frappe
from frappe.utils import get_url, getdate, nowdate

from kamra_uy.notifications.confirmation import LANGUAGE_FIELD
from kamra_uy.receipts import model

DOCTYPE = "UY Receipt"
LOG_TITLE = "kamra_uy: recibo del cobro de la reserva sin emitir"
_SAVEPOINT = "kamra_uy_receipt"
_QUIET = ("in_import", "in_install", "in_migrate", "in_patch")


def on_folio_update(doc, method=None) -> None:
	"""on_update de Folio: un recibo por cada cobro nuevo de una reserva antes del check-in, y la constancia de cada
	devolución de un pago con recibo."""
	if not doc.get("reservation") or any(frappe.flags.get(f) for f in _QUIET):
		return
	before = doc.get_doc_before_save()
	old = {p.name for p in (before.get("payments") or [])} if before else set()
	new = [p for p in doc.get("payments") or [] if p.name not in old]
	if not new:
		return
	status = frappe.db.get_value("Reservation", doc.reservation, "status")
	for row in new:
		kind = model.kind_for(row.payment_kind, row.amount, status)
		if not kind:
			continue
		refund_of = _receipt_refunded(doc, row) if kind == model.REFUND else None
		if kind == model.REFUND and not refund_of:
			continue
		_issue_safely(doc, row, kind, refund_of)


def _receipt_refunded(folio, row) -> str | None:
	"""El recibo del pago que devuelve `row`, o None si ese pago no tiene recibo."""
	target = model.refunded_payment(row.get("refund_of"), row.get("reference"))
	if not target:
		return None
	how, value = target
	if how == "reference":
		value = next((p.name for p in folio.payments if p.reference == value and p.payment_kind != "Refund"), None)
	return frappe.db.get_value(DOCTYPE, {"payment_row": value}, "name") if value else None


def _issue_safely(folio, row, kind: str, refund_of: str | None) -> None:
	frappe.db.savepoint(_SAVEPOINT)
	try:
		issue(folio, row, kind, refund_of)
	except Exception as e:
		frappe.db.rollback(save_point=_SAVEPOINT)
		# sin traza: ni el pagador ni las variables locales (HR-21)
		frappe.log_error(title=LOG_TITLE, message=type(e).__name__, reference_doctype="Folio",
		                 reference_name=folio.name)
		return
	frappe.db.release_savepoint(_SAVEPOINT)


def _next_number(prop: str) -> int:
	"""El siguiente número de la Property. Bloquea su fila hasta el final de la transacción: dos cobros a la vez
	esperan su turno y no repiten ni saltan número; si el cobro se deshace, el número vuelve a estar libre."""
	frappe.db.sql("select name from `tabProperty` where name = %s for update", prop)
	last = frappe.db.sql(f"select max(number) from `tab{DOCTYPE}` where property = %s", prop)[0][0]
	return int(last or 0) + 1


def issue(folio, row, kind: str, refund_of: str | None = None):
	"""Emite el recibo (o la constancia) de la línea `row` del folio `folio`. Idempotente por línea."""
	existing = frappe.db.get_value(DOCTYPE, {"payment_row": row.name}, "name")
	if existing:
		return frappe.get_doc(DOCTYPE, existing)
	res = frappe.db.get_value("Reservation", folio.reservation,
	                          ["name", "guest", "guest_name", "check_in_date", "check_out_date", LANGUAGE_FIELD],
	                          as_dict=True)
	prop = frappe.db.get_value("Property", folio.property,
	                           ["property_name", "legal_name", "gstin", "currency", "booking_language"], as_dict=True)
	payer = (frappe.db.get_value("Guest", res.guest, "full_name") if res.get("guest") else None) or res.guest_name
	home = (prop.currency or "").upper()
	extra = model.charged(row.get("currency"), row.get("fx_amount"), home)
	doc = frappe.get_doc({
		"doctype": DOCTYPE,
		"kind": kind,
		"number": _next_number(folio.property),
		"issued_on": getdate(row.get("posting_date") or nowdate()),
		"property": folio.property,
		"reservation": res.name,
		"folio": folio.name,
		"refund_of": refund_of,
		"issuer_name": (prop.legal_name or prop.property_name or folio.property).strip(),
		"issuer_rut": (prop.gstin or "").strip() or None,
		"payer_name": (payer or "").strip() or None,
		"check_in_date": res.check_in_date,
		"check_out_date": res.check_out_date,
		"amount": float(row.amount or 0),
		"currency": home,
		"charged_amount": (-extra["amount"] if float(row.amount or 0) < 0 else extra["amount"]) if extra else None,
		"charged_currency": extra["currency"] if extra else None,
		"exchange_rate": float(row.get("exchange_rate") or 0) or None,
		"payment_row": row.name,
		"payment_reference": row.get("reference"),
		"mode": row.get("mode"),
		"language": model.idioma(res.get(LANGUAGE_FIELD) or prop.booking_language),
		"access_key": frappe.generate_hash(length=32),
	})
	doc.receipt_no = model.number_label(doc.number)
	doc.insert(ignore_permissions=True)
	doc.db_set("receipt_url", url(doc), update_modified=False)
	return doc


def url(doc) -> str:
	"""La página del recibo para el huésped (www/recibo.py): el nombre y su clave, que no se adivina."""
	return get_url("/recibo?" + urlencode({"n": doc.name, "k": doc.access_key}))


def for_reservation(reservation: str) -> dict | None:
	"""El último recibo de un cobro de la reserva (el que la confirmó, en el correo de confirmación): {numero, url}."""
	rows = frappe.get_all(DOCTYPE, filters={"reservation": reservation, "kind": model.RECEIPT},
	                      fields=["name", "receipt_no", "access_key"], order_by="number desc", limit=1)
	if not rows:
		return None
	return {"numero": rows[0].receipt_no, "url": url(rows[0])}


def by_key(name: str | None, key: str | None) -> dict | None:
	"""El recibo `name` si `key` es su clave (comparación en tiempo constante); si no, None. Para /recibo."""
	import hmac

	if not name or not key or len(str(key)) > 64:
		return None
	row = frappe.db.get_value(DOCTYPE, str(name)[:140], "*", as_dict=True)
	if not row or not row.access_key or not hmac.compare_digest(str(row.access_key), str(key)):
		return None
	if row.refund_of:
		row["refund_of_no"] = frappe.db.get_value(DOCTYPE, row.refund_of, "receipt_no")
	return row


def reservation_dashboard(data=None):
	"""override_doctype_dashboards de Reservation: sus recibos, en las conexiones de la reserva."""
	data = frappe._dict(data or {})
	data.setdefault("transactions", [])
	data.setdefault("non_standard_fieldnames", {})
	if not data.get("fieldname"):
		data.fieldname = "reservation"
	elif data.fieldname != "reservation":
		data.non_standard_fieldnames[DOCTYPE] = "reservation"
	if not any(DOCTYPE in (g.get("items") or []) for g in data.transactions):
		data.transactions.append({"label": "Recibos", "items": [DOCTYPE]})
	return data
