# Copyright (c) 2026, Hotel Roosevelt
# License: AGPL-3.0. See license.txt

import frappe
from frappe.model.document import Document

from kamra_uy.receipts import model


class UYReceipt(Document):
	"""El recibo del hotel al cobrar una reserva antes del check-in, o la constancia de la devolución de ese pago
	(HR-349). Lo escribe solo kamra_uy.receipts.issue, con su número ya tomado; las personas lo leen y lo imprimen."""

	def autoname(self):
		code = frappe.get_cached_value("Property", self.property, "short_code") or self.property
		self.name = model.doc_name(code, self.number)


# Sin huecos en la serie: ningún rol puede crear, cambiar ni borrar un recibo (uy_receipt.json: solo leer e imprimir).

def on_doctype_update():
	# la serie de cada Property: un número, un recibo (también si dos cobros llegan a la vez)
	frappe.db.add_unique("UY Receipt", ["property", "number"], constraint_name="uy_receipt_property_number")
