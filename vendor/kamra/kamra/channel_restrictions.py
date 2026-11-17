"""The stay restrictions screen: list, save a batch, delete.

A batch is saved whole or not at all - several restrictions set at once are
one save, so a channel manager sends them as one update."""

import json

import frappe
from frappe import _

from kamra.authz import require_roles

FIELDS = ("room_type", "rate_plan", "date_from", "date_to", "days_of_week", "stop_sell",
          "closed_to_arrival", "closed_to_departure", "min_stay_arrival", "min_stay_through",
          "max_stay", "note", "disabled")


def _as_list(value) -> list:
	if isinstance(value, str):
		value = json.loads(value or "[]")
	if not isinstance(value, list):
		frappe.throw(_("Expected a list."))
	return value


@frappe.whitelist()
@require_roles("Revenue Manager", "Hotel Admin", "Front Desk")
def list_restrictions(property: str, date_from: str | None = None, date_to: str | None = None,
                      room_type: str | None = None) -> list[dict]:
	"""The property's restrictions overlapping [date_from, date_to] (either
	end open), for one room type and those for all room types, or all."""
	filters = [["property", "=", property]]
	if date_from:
		filters.append(["date_to", ">=", date_from])
	if date_to:
		filters.append(["date_from", "<=", date_to])
	or_filters = [["room_type", "=", room_type], ["room_type", "is", "not set"]] if room_type else None
	return frappe.get_all("Channel Restriction", filters=filters, or_filters=or_filters,
	                      fields=["name", *FIELDS], order_by="date_from asc, creation asc")


@frappe.whitelist(methods=["POST"])
@require_roles("Revenue Manager", "Hotel Admin")
def save_restrictions(property: str, rows: list | str) -> dict:
	"""Create (no `name`) or update (`name`) every row, in one transaction:
	one row that does not validate saves none, with that row's message."""
	rows = _as_list(rows)
	savepoint = "channel_restrictions_" + frappe.generate_hash(length=8)
	frappe.db.savepoint(savepoint)
	saved = []
	try:
		for row in rows:
			values = {k: row.get(k) for k in FIELDS if k in row}
			if row.get("name"):
				doc = frappe.get_doc("Channel Restriction", row["name"])
				if doc.property != property:
					frappe.throw(_("Restriction {0} belongs to another property.").format(row["name"]))
				doc.update(values)
				doc.save()
			else:
				doc = frappe.get_doc({"doctype": "Channel Restriction", "property": property,
				                      **values}).insert()
			saved.append(doc.name)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	return {"saved": saved}


@frappe.whitelist(methods=["POST"])
@require_roles("Revenue Manager", "Hotel Admin")
def delete_restrictions(property: str, names: list | str) -> dict:
	"""Delete the named restrictions of this property; any other is left."""
	mine = frappe.get_all("Channel Restriction",
	                      filters={"property": property, "name": ("in", _as_list(names) or [""])},
	                      pluck="name")
	for name in mine:
		frappe.delete_doc("Channel Restriction", name)
	return {"deleted": len(mine)}
