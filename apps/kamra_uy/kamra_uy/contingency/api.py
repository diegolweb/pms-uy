"""El botón de /contingencia (HR-34): «Ya lo hice en las extranets» guarda el plan como aplicado."""

from __future__ import annotations

import frappe
from frappe.utils import now_datetime
from kamra.authz import require_roles

from kamra_uy.contingency import report
from kamra_uy.pure.contingency import plan_to_json

CHANGED = "El plan cambió desde que abrió la página: recárguela y revise lo nuevo antes de marcarlo como hecho."


@frappe.whitelist(methods=["POST"])
@require_roles(*report.STAFF_ROLES)
def mark_applied(property: str, plan_hash: str | None = None) -> dict:
	"""Guarda el plan actual como lo que ya está en las extranets. Con `plan_hash` (el de la página que se leyó), se
	niega si el plan cambió entretanto: si no, se daría por hecho un cambio que nadie vio."""
	result = report.build_report(property)
	if not result["active"]:
		frappe.throw(report.OFF_MESSAGES[result["reason"]], title="Modo contingencia")
	if plan_hash and plan_hash != result["plan_hash"]:
		frappe.throw(CHANGED, title="Modo contingencia")
	snapshot = frappe.get_doc({
		"doctype": report.SNAPSHOT, "property": property, "applied_by": frappe.session.user,
		"applied_on": now_datetime(), "plan": plan_to_json(report.as_plan(result["rows"])),
	}).insert(ignore_permissions=True)
	from kamra_uy.contingency import alerts

	alerts.forget_notices(property)
	return {"snapshot": snapshot.name, "pending_changes": 0, "applied_on": str(snapshot.applied_on)}
