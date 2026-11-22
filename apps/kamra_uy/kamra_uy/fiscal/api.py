"""Lo que recepción y Finanzas piden al conector fiscal (HR-28).

- `folio_cfe` (GET): el bloque CFE del folio (serie, número, código de seguridad, CAE, QR) o su contingencia.
- `preview` (GET): antes del check-in, qué IVA llevará cada noche y por qué, y qué falta (residencia, copia).
- `process_now` (POST): emite ya lo pendiente de una reserva (lo mismo que hace la cola).
- `retry` (POST, Finanzas): vuelve a intentar un CFE que quedó para revisar.
- `record_manual` (POST, Finanzas): registra el CFE emitido en papel o en el portal de la DGI (recordManualCfe).
"""

from __future__ import annotations

import json

import frappe

from kamra.authz import require_roles
from kamra_uy.fiscal import connector, model, texts

DESK = ("Front Desk", "Finance", "Hotel Admin")
FINANCE = ("Finance", "Hotel Admin")


def _documents(reservation: str | None, folio: str | None) -> list:
	filters = {"reservation": reservation} if reservation else {"folio": folio}
	return frappe.get_all(connector.DOCTYPE, filters={**filters, "kind": ["!=", "Reconcile"]}, fields=["*"],
	                      order_by="creation asc, name asc")


def _issuer_rut(property: str) -> str | None:
	return frappe.get_cached_value("Property", property, "gstin") or None


def invoice_print_block(source_doctype: str, source_name: str, number: str | None = None) -> dict | None:
	"""Lo que la factura de un folio enseña de sus CFE. La firma es la de `invoice_print_block` de los paquetes de
	Kamra (kamra.localization), para que el paquete de Uruguay la pueda exponer tal cual."""
	if source_doctype != "Folio":
		return None
	folio = frappe.db.get_value("Folio", source_name, ["name", "reservation", "property"], as_dict=True)
	if not folio:
		return None
	rows = _documents(folio.reservation, folio.name)
	tasks = frappe.get_all(connector.DOCTYPE, filters={"reservation": folio.reservation, "kind": "Reconcile",
	                                                   "status": ["in", ["Pending", "Needs attention"]]},
	                       pluck="name") if folio.reservation else []
	if not rows and not tasks:
		return None
	rut = _issuer_rut(folio.property)
	documents = [{**model.print_block({**r, "core_status": r.core_status if r.status == "Sent" else None}, rut),
	              "name": r.name, "kamra_status": r.status, "error": r.error or ""} for r in rows]
	waiting = bool(tasks) or any(d["contingency"] for d in documents)
	attention = any(r.status == "Needs attention" for r in rows)
	return {
		"kind": "uy_cfe",
		"documents": documents,
		"contingency": waiting,
		"needs_attention": attention,
		"status_text": model.STATUS_TEXTS["pending"] if tasks or not documents else (
			next((d["status_text"] for d in documents if d["status_text"]), "")),
	}


@frappe.whitelist(methods=["GET"])
@require_roles(*DESK)
def folio_cfe(folio: str):
	frappe.get_doc("Folio", folio).check_permission("read")
	return invoice_print_block("Folio", folio) or {"kind": "uy_cfe", "documents": [], "contingency": False,
	                                              "needs_attention": False, "status_text": ""}


@frappe.whitelist(methods=["GET"])
@require_roles(*DESK)
def preview(reservation: str):
	"""Qué IVA llevaría cada noche si el check-in fuera ahora (no guarda nada)."""
	res = frappe.get_doc("Reservation", reservation)
	res.check_permission("read")
	out = connector.adjust_iva(res)
	warnings = []
	if out["residence_assumed"]:
		warnings.append(texts.RESIDENCE_MISSING)
	if not out["copy_on_file"]:
		warnings.append(texts.COPY_MISSING)
	return {"nights": [{"date": ln["date"], "kind": ln["kind"], "amount": ln["amount"], "rate": ln["rate"],
	                    "reason_code": ln["reason_code"], "pending_accountant": ln["pending_accountant"]}
	                   for ln in out["lines"]],
	        "residence_country": out["guest"]["residence_country"], "residence_assumed": out["residence_assumed"],
	        "copy_on_file": out["copy_on_file"], "buyer_type": out["buyer"]["type"], "warnings": warnings}


@frappe.whitelist(methods=["POST"])
@require_roles(*DESK)
def process_now(reservation: str):
	frappe.get_doc("Reservation", reservation).check_permission("read")
	result = connector.process_reservation(reservation)
	connector.commit()
	folio = connector._guest_folio(reservation)
	return {**result, "cfe": invoice_print_block("Folio", folio) if folio else None}


def _row(name: str):
	row = frappe.get_doc(connector.DOCTYPE, name)
	frappe.get_doc("Property", row.property).check_permission("read")
	return row


@frappe.whitelist(methods=["POST"])
@require_roles(*FINANCE, scope={"name": "UY CFE"})
def retry(name: str):
	row = _row(name)
	if row.status != "Needs attention":
		frappe.throw("Solo se reintenta un CFE que quedó para revisar.")
	frappe.db.set_value(connector.DOCTYPE, name, {"status": "Pending", "error": ""})
	return process_now(row.reservation)


@frappe.whitelist(methods=["POST"])
@require_roles(*FINANCE, scope={"name": "UY CFE"})
def record_manual(name: str, via: str, type_code: int, serie: str, number: str, issued_at: str,
                  cae_id: str | None = None, security_code: str | None = None, note: str | None = None):
	"""El CFE se hizo a mano (papel o portal de la DGI): core deja de intentarlo y Kamra guarda su serie y número."""
	row = _row(name)
	if row.kind == "Reconcile" or not row.core_cfe_id:
		frappe.throw("Este CFE todavía no está en el servicio de facturación.")
	body = {"via": via, "type_code": int(type_code), "serie": serie, "number": str(number), "issued_at": issued_at}
	body.update({k: v for k, v in {"cae_id": cae_id, "security_code": security_code, "note": note}.items() if v})
	_, client = connector.client_for(row.property)
	cfe = client.post(f"/v1/fiscal/cfe/{row.core_cfe_id}/manual-issue", body,
	                  idempotency_key=model.idempotency_key(frappe.local.site, f"{name}:manual"))
	connector.store_cfe(name, cfe, status="Sent")
	frappe.db.set_value(connector.DOCTYPE, name, "request_json",
	                    json.dumps({**json.loads(row.request_json or "{}"), "manual": body}))
	return model.cfe_fields(cfe)
