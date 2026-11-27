"""Los destinatarios del modo contingencia de una propiedad (HR-374; D-HR-65, H5: «a través de reservas@»).

Sin «UY Contingency Settings», los avisos del modo (los cambios en las extranets y el informe del día, `alerts.py`) no
salen hacia nadie: si Channex cae, recepción no recibe lo que hay que poner a mano en las extranets. Lo llama el
despliegue de producción en cada --sha (deploy/prod/lib/prod.sh: poner_contingencia), con `bench execute`, así que
tiene que poder repetirse sin cambiar nada:

- sin ajuste, lo crea activado, con esos destinatarios y lo demás por defecto (sin cupos: D-HR-70);
- con ajuste y sin destinatarios, se los pone (y no toca nada más);
- con ajuste y destinatarios, no lo toca: si ya están, «igual»; si son otros, los puso alguien a mano, y manda él.
"""

import frappe

from kamra_uy.contingency import report
from kamra_uy.pure.contingency import split_recipients

STATES = ("creado", "puesto", "igual", "otros")


def ensure_recipients(property: str, recipients: str) -> str:
	"""Deja la propiedad con destinatarios del modo contingencia. Devuelve uno de STATES."""
	wanted = split_recipients(recipients)
	if not wanted:
		frappe.throw("Faltan los destinatarios del modo contingencia.")
	if not property or not frappe.db.exists("Property", property):
		frappe.throw(f"No existe la propiedad «{property}».")
	settings = report.settings_for(property)
	if settings is None:
		frappe.get_doc({"doctype": report.SETTINGS, "property": property, "enabled": 1,
		                "recipients": ", ".join(wanted)}).insert(ignore_permissions=True)
		return "creado"
	current = split_recipients(settings.recipients)
	if not current:
		settings.recipients = ", ".join(wanted)
		settings.save(ignore_permissions=True)
		return "puesto"
	have = {r.lower() for r in current}
	return "igual" if all(r.lower() in have for r in wanted) else "otros"
