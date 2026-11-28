"""HR-380: las reservas de plataforma que ya estaban cargadas pasan a «la cobra el hotel».

Antes de HR-380, Kamra no leía de Channex quién cobra una reserva (`payment_collect`, `payment_type`): toda reserva
de canal entraba con `is_pay_at_hotel` 0 y el check-in decía «la OTA ya cobró». Hoy las tres plataformas del hotel
las cobra el hotel, así que las vivas sin el dato (`ota_collect` vacío) quedan como `Hotel`, cobrables desde el día en
que entraron (D-HR-95: lo antes posible) y con la cancelación gratis de la regla de Kamra (`free_cancel_days` de su
propiedad), la misma que aplica `kamra.channel_payments` cuando Channex no manda penalización. Airbnb no: la cobra
siempre la plataforma (D-S4-4), así que las suyas quedan `OTA`, sin `is_pay_at_hotel` ni fecha de cobro (si no,
RES-2026-00004, pagada en Airbnb, saldría «por cobrar»). La de Airbnb que ya está en casa el día del despliegue
recibe, además, la línea `OTA Prepaid` que el check-in le habría puesto (R5, `post_platform_payment`): sin ella, su
folio diría que el huésped debe la habitación; si su folio ya tiene algún pago, no se le pone (sería el doble) y el patch
imprime cuáles son, para que recepción las repase. Las que ya tienen el dato no se tocan y la línea va por su clave, así
que el patch es idempotente; `db_set` sin `modified`. Va en [post_model_sync] de patches.txt: `bench migrate` crea
antes los tres campos del doctype.
"""

from __future__ import annotations

import frappe
from frappe.utils import getdate
from kamra.channel_manager import canonical_channel
from kamra.channel_payments import HOTEL, PLATFORM, PLATFORM_ALWAYS, free_cancel_until_of, post_platform_payment

ESTADOS = ("Requested", "Confirmed", "Checked In")


def execute() -> None:
	filas = frappe.get_all(
		"Reservation",
		filters={"source": "OTA", "ota_ref": ("is", "set"), "ota_collect": ("is", "not set"),
		         "status": ("in", ESTADOS)},
		fields=["name", "property", "channel", "check_in_date", "creation", "status"])
	dias: dict[str, int | None] = {}
	saltadas: list[str] = []
	for fila in filas:
		if fila.property not in dias:
			valor = frappe.db.get_value("Property", fila.property, "free_cancel_days")
			dias[fila.property] = None if valor is None else int(valor)
		alta = getdate(fila.creation)
		plataforma = canonical_channel(fila.channel) in PLATFORM_ALWAYS
		frappe.db.set_value("Reservation", fila.name, {
			"ota_collect": PLATFORM if plataforma else HOTEL,
			"is_pay_at_hotel": 0 if plataforma else 1,
			"ota_charge_from": None if plataforma else alta,
			"ota_free_cancel_until": free_cancel_until_of(None, fila.check_in_date, dias[fila.property], alta, None),
		}, update_modified=False)
		if plataforma and fila.status == "Checked In":
			# ya en casa: el check-in no le puso la línea (R5); se la pone ahora, una vez por su clave. Si su folio ya
			# tiene algún pago (recepción pudo apuntar a mano el de Airbnb), no: sería el doble; se lista para repasarla
			folios = frappe.get_all("Folio", filters={"reservation": fila.name}, pluck="name")
			if folios and frappe.db.exists("Folio Payment", {"parent": ("in", folios)}):
				saltadas.append(fila.name)
				continue
			post_platform_payment(frappe.get_doc("Reservation", fila.name))
	if saltadas:
		print(f"cobro_plataformas: {len(saltadas)} de Airbnb en casa ya tienen pagos en el folio, "
		      f"sin la línea OTA Prepaid (repasarlas): {', '.join(saltadas)}")
