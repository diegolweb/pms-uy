"""Monitoreo y alertas de Kamra (HR-21).

- `status` (/api/method/kamra_uy.health.status, GET, abierto): el estado de cada señal (checks.py). Sin más, solo
  ok/fail/off por señal, para un monitor externo; con la cabecera X-HR-Health-Token igual a `hr_health_token` de
  site_config.json (o con sesión de System Manager), también el detalle de cada una.
- beat.py: el latido del programador (cada minuto, desde hooks.py).
- sentry.py y scrub.py: los errores a Sentry sin datos personales.
- vigia.py: quien mira todo esto desde fuera y avisa (correo, WhatsApp, Slack); corre fuera de Kamra
  (deploy/monitoring).
"""

import frappe

from kamra_uy.health import checks


@frappe.whitelist(allow_guest=True, methods=["GET"])
def status() -> dict:
	full = checks.cached_report()
	return full if checks.may_see_details() else checks.public_view(full)
