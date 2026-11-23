"""/facturas (HR-304): las facturas (CFE) que el sistema aún no emitió, mientras no hay conexión con Gsoft,
con lo que hay que copiar en el portal de Factura Express, y el formulario para registrar la serie y el número que dio
el portal (fiscal.api.record_manual): así, al conectar Gsoft, no salen dos veces. Arriba, las reservas cuya factura
espera a la residencia del huésped. Solo personal del hotel (Front Desk, Finance, Hotel Admin o un administrador) y
solo las propiedades de Uruguay que puede ver."""

from __future__ import annotations

import re

import frappe

from kamra_uy.contingency import report
from kamra_uy.fiscal import api, connector

no_cache = 1
sitemap = 0

ROLES = ("Front Desk", "Finance", "Hotel Admin")
VIAS = (("portal", "Portal de Factura Express"), ("paper", "Papel"))


def _allowed() -> bool:
	from kamra.authz import ADMIN

	return bool(set(ROLES + tuple(ADMIN)) & set(frappe.get_roles()))


def _choices() -> list[str]:
	from kamra.authz import restricted_properties

	allowed = restricted_properties()
	names = [p for p in frappe.get_all("Property", pluck="name", order_by="name") if connector.is_uruguay(p)]
	return names if allowed is None else [p for p in names if p in allowed]


def get_context(context):
	if frappe.session.user == "Guest" or not _allowed():
		raise frappe.PermissionError("Solo el personal del hotel puede ver las facturas pendientes.")
	context.title = "Facturas pendientes"
	context.no_breadcrumbs = True
	context.csrf_token = frappe.sessions.get_csrf_token()
	context.vias = VIAS
	day = (frappe.form_dict.get("fecha") or "").strip()
	context.day = day if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else ""
	choices = _choices()
	requested = frappe.form_dict.get("property")
	property = report.property_for_user(requested) if requested else (choices[0] if len(choices) == 1 else None)
	if property and not connector.is_uruguay(property):
		property = None
	context.property = property
	context.choices = [] if property else choices
	context.items, context.residence_missing = [], []
	if property:
		data = api.pending(property, context.day or None)
		context.items, context.residence_missing = data["items"], data["residence_missing"]
	return context
