# One stay restriction: for a date range (and optionally some weekdays), a
# room type or all, a rate plan or all - stop sell, closed to arrival or
# departure, the minimum stay on arrival or through, the maximum stay.
# pricing.stay_rules_by_day reads these rows with the seasons, so the booking
# page, the front desk and the channel managers apply one set of rules.

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, getdate

NUMBERS = ("min_stay_arrival", "min_stay_through", "max_stay")


def _number_name(field: str) -> str:
	"""The name the restrictions screen gives each number, in the desk's
	language (the doctype labels are the Desk's, untranslated)."""
	return {"min_stay_arrival": _("Min. stay on arrival"),
	        "min_stay_through": _("Min. stay through"),
	        "max_stay": _("Max. stay")}[field]


class ChannelRestriction(Document):
	def validate(self):
		if getdate(self.date_to) < getdate(self.date_from):
			frappe.throw(_("The end date cannot be before the start date."))
		for field in NUMBERS:
			if cint(self.get(field)) < 0:
				frappe.throw(_("{0} cannot be negative.").format(_number_name(field)))
		mins = max(cint(self.min_stay_arrival), cint(self.min_stay_through))
		if not (self.stop_sell or self.closed_to_arrival or self.closed_to_departure
		        or mins > 1 or cint(self.max_stay) > 0):
			frappe.throw(_("Set at least one restriction: stop sell, closed to arrival, "
			               "closed to departure, a minimum stay of two nights or more, "
			               "or a maximum stay."))
		if cint(self.max_stay) == 1 and mins > 1:
			frappe.throw(_("The maximum stay (1 night) cannot be shorter than the "
			               "minimum stay ({0} nights).").format(mins))
		if cint(self.max_stay) and cint(self.max_stay) < mins:
			frappe.throw(_("The maximum stay ({0} nights) cannot be shorter than the "
			               "minimum stay ({1} nights).").format(cint(self.max_stay), mins))
		for field, doctype, message in (
			("room_type", "Room Type", _("Room type {0} belongs to another property.")),
			("rate_plan", "Rate Plan", _("Rate plan {0} belongs to another property.")),
		):
			value = self.get(field)
			if value and frappe.db.get_value(doctype, value, "property") != self.property:
				frappe.throw(message.format(value))
