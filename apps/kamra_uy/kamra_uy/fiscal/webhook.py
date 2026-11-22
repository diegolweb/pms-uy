"""El receptor de `cfe.updated` (HR-26 → HR-28): serie, número, CAE y QR del CFE, o su contingencia, en UY CFE.

`POST /api/method/kamra_uy.fiscal.webhook.core_event` (el `x-hr-target-path` de cfeUpdated en services/core/openapi.yaml),
firmado con `X-HR-Signature` con el mismo secreto que los avisos de cobros (Payment Gateway Settings.webhook_secret).
Respuestas: cuerpo que no es un aviso → 400; firma mala, propiedad desconocida, `X-HR-Event-Id` cambiado o CFE de otra
propiedad → 403; CFE que Kamra no pidió → 200 {"ignored": true}; aviso igual o más viejo que lo guardado → 200
{"duplicate": true}. Core reintenta todo lo que no sea 2xx.
"""

from __future__ import annotations

import time

import frappe

from kamra_uy.fiscal import connector, model
from kamra_uy.pure.core_signature import verify


class InvalidEvent(frappe.ValidationError):
	http_status_code = 400


@frappe.whitelist(allow_guest=True, methods=["POST"])
def core_event():
	headers = {k.lower(): v for k, v in (frappe.request.headers or {}).items()}
	out = receive(frappe.request.get_data() or b"", headers)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persists the CFE state before answering core (external caller)
	return out


def receive(payload: bytes, headers: dict) -> dict:
	try:
		event = model.parse_cfe_event(payload)
	except ValueError as e:
		frappe.throw(str(e), InvalidEvent)
	name = frappe.db.get_value("Payment Gateway Settings", {"gateway": connector.GATEWAY, "key_id": event["property"]})
	settings = frappe.get_doc("Payment Gateway Settings", name) if name else None
	secret = settings.get_password("webhook_secret", raise_exception=False) if settings else None
	ok = verify(secret, headers.get("x-hr-signature"), payload, int(time.time()))[0] if secret else False
	if not ok:
		frappe.throw("Invalid webhook signature", frappe.PermissionError)
	if headers.get("x-hr-event-id") != event["event_id"]:
		frappe.throw("The event id does not match the signed event", frappe.PermissionError)
	cfe = event["cfe"]
	row = frappe.db.get_value(connector.DOCTYPE, {"core_cfe_id": cfe["cfe_id"]},
	                          ["name", "property", "core_updated_at"], as_dict=True)
	if not row:
		return {"ignored": True}
	if row.property != settings.property:
		frappe.throw("This CFE belongs to another property", frappe.PermissionError)
	if not model.is_newer(cfe.get("updated_at"), row.core_updated_at):
		return {"duplicate": True}
	connector.store_cfe(row.name, cfe)
	return {"ok": True, "status": cfe["status"]}
