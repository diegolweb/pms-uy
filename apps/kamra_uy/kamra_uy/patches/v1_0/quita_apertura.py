"""HR-275 (D-HR-49, 22-11): quita de la base lo que dejaron la fecha de apertura y el cierre de ventas antes de abrir
(HR-263 y HR-264), que ya no están en el código. El fundador: «YO decido las fechas»; «Quítalo del todo».

- Las Channel Restriction de kamra_uy (`UY-APERTURA-<propiedad>`, el stop sell de hoy a la víspera de la apertura): se
  borran con `frappe.delete_doc`, así que el outbox de ARI de Kamra (sus doc_events) le manda a Channex que esas noches
  vuelven a la venta. Las puestas a mano (otro nombre) no se tocan.
- El Custom Field `uy_opening_date` de la Property y su columna (Frappe no borra la columna al borrar el campo).

Idempotente: lo que ya no está, no se toca. Va en [post_model_sync] de patches.txt.
"""

from __future__ import annotations

import frappe

CUSTOM_FIELD = "Property-uy_opening_date"
FIELD = "uy_opening_date"
PREFIX = "UY-APERTURA-"
DOCTYPE = "Channel Restriction"


def restrictions() -> list[str]:
	"""Las filas de kamra_uy que quedan (el prefijo, en mayúsculas o minúsculas, como las reconocía HR-264)."""
	if not frappe.db.table_exists(DOCTYPE):
		return []
	return [n for n in frappe.get_all(DOCTYPE, filters={"name": ("like", PREFIX + "%")}, pluck="name")
	        if n.upper().startswith(PREFIX)]


def execute() -> None:
	for name in restrictions():
		frappe.delete_doc(DOCTYPE, name, ignore_permissions=True)
	if frappe.db.exists("Custom Field", CUSTOM_FIELD):
		frappe.delete_doc("Custom Field", CUSTOM_FIELD, ignore_permissions=True)
	if frappe.db.has_column("Property", FIELD):
		frappe.db.sql_ddl(f"ALTER TABLE `tabProperty` DROP COLUMN `{FIELD}`")
	frappe.clear_cache(doctype="Property")
