"""El barrido del conector fiscal (HR-28), cada 5 minutos: lo arranca el scheduler de Frappe desde hooks.py
(scheduler_events, cron «*/5 * * * *»).

1. Reintenta lo pendiente: las reservas con filas «Pending» en UY CFE (core caído, cola perdida, sin credenciales).
2. Se pone al día: un CFE en cola o en contingencia que no cambió en 10 minutos se pregunta a core (getCfe), por si
   se perdió su aviso `cfe.updated` (Revisión G: un aviso agotado en el outbox de core no vuelve).
"""

from __future__ import annotations

import frappe
from frappe.utils import add_to_date, now_datetime

from kamra_uy.fiscal import connector

BATCH = 50
STALE_MINUTES = 10


def sweep() -> dict:
	done = {"reservations": 0, "refreshed": 0, "failed": 0}
	reservations = frappe.get_all(connector.DOCTYPE, filters={"status": "Pending"}, pluck="reservation",
	                              order_by="creation asc", limit=BATCH * 4)
	for name in list(dict.fromkeys(r for r in reservations if r))[:BATCH]:
		try:
			connector.process_reservation(name)
			connector.commit()
			done["reservations"] += 1
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title="kamra_uy fiscal: barrido")
			done["failed"] += 1
	stale = frappe.get_all(connector.DOCTYPE, filters={
		"status": "Sent", "core_status": ["in", ["queued", "contingency"]],
		"modified": ["<", add_to_date(now_datetime(), minutes=-STALE_MINUTES)]}, pluck="name", limit=BATCH * 2)
	for name in stale:
		try:
			connector.refresh(name)
			frappe.db.set_value(connector.DOCTYPE, name, "modified", now_datetime(), update_modified=False)
			connector.commit()
			done["refreshed"] += 1
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title="kamra_uy fiscal: poner al día un CFE")
			done["failed"] += 1
	return done
