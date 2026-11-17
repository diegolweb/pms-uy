# Copyright (c) 2026, Hotel Roosevelt
# License: AGPL-3.0. See license.txt

from frappe.model.document import Document


class UYContingencySettings(Document):
	def validate(self):
		from kamra_uy.contingency.report import validate_settings

		validate_settings(self)
