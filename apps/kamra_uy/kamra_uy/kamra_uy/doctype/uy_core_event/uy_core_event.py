# Copyright (c) 2026, Hotel Roosevelt
# License: AGPL-3.0. See license.txt

from frappe.model.document import Document


class UYCoreEvent(Document):
	"""Un aviso firmado del servicio de cobros y qué se hizo con él (HR-30). Lo escribe solo
	kamra_uy.payments.webhook; las personas lo leen."""
