"""Lo que recepción y Finanzas piden al conector fiscal (HR-28).

- `folio_cfe` (GET): el bloque CFE del folio (serie, número, código de seguridad, CAE, QR) o su contingencia.
- `preview` (GET): antes del check-in, qué IVA llevará cada noche y por qué, y qué falta (residencia, copia).
- `process_now` (POST): emite ya lo pendiente de una reserva (lo mismo que hace la cola).
- `retry` (POST, Finanzas): vuelve a intentar un CFE que quedó para revisar.
- `record_manual` (POST, Finanzas): registra el CFE emitido en papel o en el portal de la DGI (recordManualCfe).
- `invoice_currency` y `set_invoice_currency` (HR-316; kamra.localization, «the currency a stay is invoiced in»): la
  moneda de la factura, que recepción elige en la última pantalla del check-in (dólares o pesos; por defecto, dólares
  en la alta temporada y pesos el resto del año). Los precios siguen en dólares (D-HR-60).
"""

from __future__ import annotations

import json
from urllib.parse import quote

import frappe

from kamra.authz import require_roles
from kamra_uy.fiscal import connector, forfeit, model, texts

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
	                       fields=["name", "error"]) if folio.reservation else []
	# HR-304: el que espera a la residencia del huésped lo dice, no «el servicio no respondió»
	residence = any((t.error or "") == texts.RESIDENCE_MISSING for t in tasks)
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
		"residence_missing": residence,
		"status_text": texts.RESIDENCE_MISSING if residence else model.STATUS_TEXTS["pending"] if tasks or not documents else (
			next((d["status_text"] for d in documents if d["status_text"]), "")),
	}


def pending_url(property: str) -> str:
	"""/facturas de esa propiedad (www/facturas.py)."""
	return f"/facturas?property={quote(property)}"


def folio_fiscal_documents(folio: str) -> dict | None:
	"""La tarjeta del CFE en el folio de recepción (HR-307), con la firma de `folio_fiscal_documents` de los paquetes
	de Kamra (kamra.localization), que la pinta aparte de la factura impresa: estado, tipo, serie y número, y el aviso
	de la residencia (`model.desk_block`). Sin CFE ni tarea de la reserva, None."""
	row = frappe.db.get_value("Folio", folio, ["name", "reservation", "property"], as_dict=True)
	if not row:
		return None
	tasks = frappe.get_all(connector.DOCTYPE, filters={"reservation": row.reservation, "kind": "Reconcile",
	                                                   "status": ["in", ["Pending", "Needs attention"]]},
	                       fields=["status", "error"]) if row.reservation else []
	return model.desk_block(_documents(row.reservation, row.name), tasks, pending_url(row.property))


def desk_pages(property: str) -> list[dict]:
	"""Lo que recepción abre desde el menú de Kamra (`desk_pages` de kamra.localization): /facturas, con los roles
	que la página deja entrar (www/facturas.py)."""
	from kamra.authz import ADMIN

	roles = list(dict.fromkeys(DESK + tuple(ADMIN)))
	return [{"label": model.PENDING_PAGE, "url": pending_url(property), "roles": roles}]


@frappe.whitelist(methods=["GET"])
@require_roles(*DESK)
def folio_cfe(folio: str):
	frappe.get_doc("Folio", folio).check_permission("read")
	return invoice_print_block("Folio", folio) or {"kind": "uy_cfe", "documents": [], "contingency": False,
	                                              "needs_attention": False, "residence_missing": False,
	                                              "status_text": ""}


def invoice_currency(reservation: str) -> dict | None:
	"""La elección de la moneda de la factura para el check-in (HR-316): las dos monedas, la propuesta por la fecha
	del check-in y, en cada una, con qué cotización del BCU sale. Fuera de Uruguay, o con una propiedad en una moneda
	que no se factura con CFE, nada que elegir."""
	res = frappe.get_doc("Reservation", reservation)
	if not connector.is_uruguay(res.property):
		return None
	try:
		home = connector._currency(res.property)
	except connector.FiscalError:
		return None
	try:
		fx, day = connector.fx_quote(res.property, "USD")
	except connector.FiscalError:
		fx, day = None, None
	return model.currency_choice(home, res.check_in_date, fx, day)


def set_invoice_currency(doc, currency: str) -> None:
	"""La moneda que eligió recepción, en la reserva que kamra.api.check_in va a guardar con el huésped en casa: la
	tarea del CFE del check-in la toma de ahí (connector.queue_task)."""
	if currency not in model.INVOICE_CURRENCIES:
		frappe.throw(texts.BAD_CURRENCY.format(currency=currency or "-"))
	doc.flags.invoice_currency = currency


@frappe.whitelist(methods=["GET"])
@require_roles(*DESK)
def preview(reservation: str):
	"""Qué IVA llevaría cada noche si el check-in fuera ahora (no guarda nada). Sin la residencia del huésped no hay
	noches que enseñar: `residence_missing` y el aviso (HR-304: nunca se supone)."""
	res = frappe.get_doc("Reservation", reservation)
	res.check_permission("read")
	try:
		out = connector.adjust_iva(res)
	except connector.ResidenceMissingError:
		return {"nights": [], "residence_country": None, "document_country": None, "residence_missing": True,
		        "copy_on_file": None,
		        "buyer_type": None, "warnings": [texts.RESIDENCE_MISSING]}
	warnings = [] if out["copy_on_file"] else [texts.COPY_MISSING]
	return {"nights": [{"date": ln["date"], "kind": ln["kind"], "amount": ln["amount"], "rate": ln["rate"],
	                    "reason_code": ln["reason_code"], "pending_accountant": ln["pending_accountant"]}
	                   for ln in out["lines"]],
	        "residence_country": out["guest"].get("residence_country"), "document_country": out["document_country"],
	        "residence_missing": False,
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
@require_roles(*DESK, scope={"name": "UY CFE"})
def record_manual(name: str, via: str, type_code: int, serie: str, number: str, issued_at: str,
                  cae_id: str | None = None, security_code: str | None = None, note: str | None = None):
	"""El CFE se hizo a mano (portal de Factura Express o papel): core deja de intentarlo y Kamra guarda su serie y
	número. Lo hace recepción desde /facturas (HR-304), para que al conectar Gsoft no salga dos veces."""
	row = _row(name)
	if row.kind == "Reconcile" or not row.core_cfe_id:
		frappe.throw(texts.NOT_IN_CORE_YET)
	if not model.is_pending(row):
		frappe.throw(texts.ALREADY_ISSUED)
	try:
		body = model.manual_issue(row.cfe_type, via, type_code, serie, number, issued_at, note)
	except ValueError as e:
		frappe.throw(str(e))
	body.update({k: v for k, v in {"cae_id": cae_id, "security_code": security_code}.items() if v})
	_, client = connector.client_for(row.property)
	cfe = client.post(f"/v1/fiscal/cfe/{row.core_cfe_id}/manual-issue", body,
	                  idempotency_key=model.idempotency_key(frappe.local.site, f"{name}:manual"))
	connector.store_cfe(name, cfe, status="Sent")
	frappe.db.set_value(connector.DOCTYPE, name, "request_json",
	                    json.dumps({**json.loads(row.request_json or "{}"), "manual": body}))
	return model.cfe_fields(cfe)


def pending(property: str, day: str | None = None) -> dict:
	"""Lo que /facturas enseña de una propiedad (HR-304): los CFE sin emitir (`model.is_pending`; con
	`day`, solo los pedidos ese día, la lista del día para el portal) con lo que hay que copiar en el portal, y las
	reservas cuyo comprobante espera a la residencia del huésped."""
	rows = frappe.get_all(connector.DOCTYPE, filters={"property": property, "kind": ["!=", "Reconcile"]},
	                      fields=["name", "kind", "status", "core_status", "reservation", "cfe_type", "currency",
	                              "total", "lines_json", "core_cfe_id", "contingency_since", "overdue", "error",
	                              "creation", "reference_number", "request_json", "references_cfe",
	                              "source_currency", "source_fx_rate"],
	                      order_by="creation asc, name asc")
	rows = [r for r in rows if model.is_pending(r) and (not day or str(r.creation)[:10] == day)]
	guests = dict(frappe.get_all("Reservation", filters={"name": ["in", [r.reservation for r in rows] or [""]]},
	                             fields=["name", "guest_name"], as_list=True))
	items = []
	for r in rows:
		request = json.loads(r.request_json or "{}")
		if r.kind == "Credit note" and r.references_cfe:
			# la nota de crédito va en la moneda y con el tipo de cambio de su factura (HR-316)
			original = frappe.db.get_value(connector.DOCTYPE, r.references_cfe,
			                               ["request_json", "source_currency", "source_fx_rate"], as_dict=True) or {}
			fx = model.fx_note(r.currency, original.get("source_currency"),
			                   json.loads(original.get("request_json") or "{}").get("fx_rate"),
			                   original.get("source_fx_rate"))
		else:
			fx = model.fx_note(r.currency or request.get("currency"), r.source_currency, request.get("fx_rate"),
			                   r.source_fx_rate)
		items.append({
			"name": r.name, "reservation": r.reservation, "guest_name": guests.get(r.reservation) or "",
			"kind": r.kind, "title": model.TYPE_TITLES.get(r.cfe_type or "", texts.TYPE_PENDING),
			"currency": r.currency or request.get("currency") or "", "total": r.total or "", "fx": fx,
			"created": str(r.creation)[:16], "buyer": request.get("buyer") or {},
			"lines": [{"description": ln.get("description"), "amount": ln.get("unit_amount") or ln.get("amount"),
			           "rate": ln.get("rate")}
			          for ln in json.loads(r.lines_json or "[]")],
			"in_core": bool(r.core_cfe_id), "overdue": bool(r.overdue),
			# la seña sin el documento del comprador (HR-347) dice qué falta, no «todavía no llegó»
			"state": forfeit.DOC_MISSING if r.error == forfeit.DOC_MISSING else
			texts.NOT_IN_CORE_YET if not r.core_cfe_id else model.STATUS_TEXTS.get(r.core_status or "", ""),
			"codes": model.type_codes(r.cfe_type),
		})
	waiting = frappe.get_all(connector.DOCTYPE, filters={"property": property, "kind": "Reconcile", "status": "Pending",
	                                                     "error": texts.RESIDENCE_MISSING},
	                         fields=["reservation"], distinct=True, pluck="reservation")
	return {"items": items, "residence_missing": sorted(set(waiting))}
