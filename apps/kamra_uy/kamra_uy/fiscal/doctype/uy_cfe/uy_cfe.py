"""Un CFE pedido al servicio de facturación (original o nota de crédito), o la tarea que decide cuáles hacen falta
(HR-28; la de la seña retenida, HR-347, espera «Queued» y la procesa fiscal/forfeit.py). Lo escribe solo el conector (kamra_uy/fiscal/connector.py): para las personas es de solo lectura."""

from frappe.model.document import Document


class UYCFE(Document):
	pass
