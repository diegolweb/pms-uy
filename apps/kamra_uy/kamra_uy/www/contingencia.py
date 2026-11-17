"""/contingencia (HR-34): qué abrir y qué cerrar en cada extranet mientras no haya sincronización. Solo personal del
hotel (Front Desk, Hotel Admin o un administrador); un invitado o un usuario sin rol recibe 403."""

from __future__ import annotations

import frappe
from frappe.utils import getdate

from kamra_uy.contingency import report
from kamra_uy.pure.contingency import action_label

no_cache = 1
sitemap = 0

WEEKDAYS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")


def _platforms(rows: list[dict]) -> list[str]:
	"""Las columnas: cada plataforma una vez, en el orden en que aparece (el de prioridad)."""
	seen: list[str] = []
	for r in rows:
		for i in r["instructions"]:
			if i.platform not in seen:
				seen.append(i.platform)
	return seen


def _view(row: dict, platforms: list[str]) -> dict:
	day = getdate(row["date"])
	by_platform = {i.platform: i for i in row["instructions"]}
	cells = []
	for name in platforms:
		instruction = by_platform.get(name)
		cells.append({
			"label": action_label(instruction) if instruction else "—",
			"open": bool(instruction and instruction.action == "open"),
			"sold": row["sold"].get(name),
		})
	return {**row, "day": f"{WEEKDAYS[day.weekday()]} {day.strftime('%d-%m-%Y')}", "cells": cells}


def get_context(context):
	if frappe.session.user == "Guest" or not report.staff_roles_ok():
		raise frappe.PermissionError("Solo el personal del hotel puede ver el modo contingencia.")
	context.title = "Modo contingencia"
	context.no_breadcrumbs = True
	context.csrf_token = frappe.sessions.get_csrf_token()
	property = report.property_for_user(frappe.form_dict.get("property"))
	context.property = property
	context.choices = [] if property else frappe.get_all(report.SETTINGS, pluck="property", order_by="property")
	context.off_message = ""
	context.rows = []
	context.platforms = []
	context.report = None
	if not property:
		return context
	result = report.build_report(property)
	context.report = result
	if not result["active"]:
		context.off_message = report.OFF_MESSAGES[result["reason"]]
		return context
	context.missing_quotas = report.MISSING_QUOTAS if result["missing_quotas"] else ""
	platforms = _platforms(result["rows"])
	context.platforms = platforms
	# lo que cambió, arriba; dentro de cada grupo, por fecha y tipo
	ordered = sorted(result["rows"], key=lambda r: (not r["changed"], r["date"], r["room_type_name"]))
	context.rows = [_view(r, platforms) for r in ordered]
	return context
