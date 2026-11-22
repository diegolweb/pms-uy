"""Los ganchos de documento del conector fiscal (HR-28), registrados en hooks.py (doc_events).

Ninguno impide trabajar a recepción: un fallo queda en el registro de errores y en UY CFE, nunca en una excepción
que pare el check-in, el cierre o la anulación (como los ganchos de factura electrónica de kamra.localization).
"""

from __future__ import annotations

import frappe

from kamra_uy.fiscal import connector, texts

# lo que, si cambia con el huésped en casa, cambia el IVA de las noches
IVA_INPUTS = ("status", "check_in_date", "check_out_date", "guest", "id_document", "id_document_discarded",
              "company", "room_type")
GUEST_INPUTS = ("residence_country", "id_type", "id_number", "id_file")


def _safe(title: str, fn, *args):
	sp = "kamra_uy_fiscal"
	frappe.db.savepoint(sp)
	try:
		return fn(*args)
	except Exception:
		frappe.db.rollback(save_point=sp)
		frappe.log_error(title=f"kamra_uy fiscal: {title}")
		return None


def _changed(doc, fields) -> bool:
	before = doc.get_doc_before_save()
	return before is None or any(before.get(f) != doc.get(f) for f in fields)


def on_reservation_validate(doc, method=None):
	"""El IVA de cada noche, antes de que Kamra abra el folio y cobre nada (handle_check_in va en on_update)."""
	if doc.status != "Checked In" or not _changed(doc, IVA_INPUTS) or not connector.is_uruguay(doc.property):
		return
	try:
		connector.adjust_iva(doc)
	except Exception:
		# core caído: las noches siguen con el IVA de la cotización (0, HR-239); la tarea del CFE del check-in lo vuelve
		# a preguntar y se lo pone (connector._run_task)
		frappe.log_error(title="kamra_uy fiscal: ajuste de IVA en el check-in")


def on_reservation_update(doc, method=None):
	before = doc.get_doc_before_save()
	was = before.status if before else None
	if doc.status == "Checked In" and was != "Checked In":
		_safe("CFE del check-in", connector.queue_task, doc, "Check-in")
	elif was == "Checked In" and doc.status in ("Confirmed", "Cancelled", "No Show"):
		_safe("check-in deshecho", connector.queue_task, doc, "Void", None, texts.REASON_CHECK_IN_UNDONE)


def on_folio_update(doc, method=None):
	before = doc.get_doc_before_save()
	if doc.status != "Closed" or (before and before.status == "Closed") or not doc.reservation:
		return
	if doc.get("folio_type") == "Account":
		return
	res = frappe.get_doc("Reservation", doc.reservation)
	_safe("ajuste al cerrar la cuenta", connector.queue_task, res, "Close", doc.name)


def on_invoice_cancelled(doc, method=None):
	"""Cancelled Invoice (kamra.folio.cancel_invoice): la nota de crédito de lo que ya no está facturado."""
	reservation = frappe.db.get_value("Folio", doc.folio, "reservation") if doc.folio else None
	if not reservation:
		return
	res = frappe.get_doc("Reservation", reservation)
	reason = texts.REASON_VOID.format(reason=(doc.reason or "").strip()) if doc.reason else texts.REASON_CHECK_IN_UNDONE
	_safe("nota de crédito al anular", connector.queue_task, res, "Void", doc.folio, reason)


def on_guest_update(doc, method=None):
	"""Se corrigió la residencia o el documento de un huésped en casa: el IVA de las noches que faltan."""
	if not _changed(doc, GUEST_INPUTS):
		return
	for name in frappe.get_all("Reservation", filters={"guest": doc.name, "status": "Checked In"}, pluck="name"):
		if connector.is_uruguay(frappe.db.get_value("Reservation", name, "property")):
			_safe("ajuste de IVA del huésped", connector.readjust, name)
