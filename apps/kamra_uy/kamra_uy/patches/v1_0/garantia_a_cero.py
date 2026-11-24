"""HR-232 (D-HR-64, el fundador, 23-11: «Ponlo en 0, no pedimos garantía»): las propiedades ya cargadas (la de
producción y `HR-DEV`) dejan de pedir depósito de garantía en el próximo despliegue, que corre `bench migrate`.

Con la regla de la carga (`kamra_uy.setup.plan.garantia_de_fabrica`): solo la que sigue con el de fábrica de Kamra
(5000); una puesta a mano no se toca. Un patch corre una vez por sitio, así que tampoco pisa la que alguien ponga
después. Va en [post_model_sync] de patches.txt.
"""

from __future__ import annotations

import frappe

from kamra_uy.setup.plan import GARANTIA, garantia_de_fabrica

CAMPO = "security_deposit_amount"


def execute() -> None:
	for fila in frappe.get_all("Property", fields=["name", CAMPO]):
		if garantia_de_fabrica(fila.get(CAMPO)):
			frappe.db.set_value("Property", fila.name, CAMPO, GARANTIA)
			# kamra.deposit lee la política con get_cached_value
			frappe.clear_document_cache("Property", fila.name)
