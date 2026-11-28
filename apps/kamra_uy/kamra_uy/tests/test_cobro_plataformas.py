"""HR-380: el patch que deja las reservas de plataforma ya cargadas como «la cobra el hotel».

Una reserva de canal viva sin el dato de quién cobra pasa a `Hotel`, con `is_pay_at_hotel` 1, cobrable desde el día
en que entró y con la cancelación gratis de la regla de Kamra; la que ya tiene el dato y la que no es de plataforma
no se tocan, y correrlo dos veces da lo mismo. Sin DDL: cabe en el savepoint de AuditTestCase.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, getdate, nowdate
from kamra.tests.audit_fixtures import AuditTestCase, guest_folio, make_property, make_reservation, make_room, make_room_type

from kamra_uy.patches.v1_0 import cobro_plataformas as patch

PROP = "UT Cobro Plataformas"


class TestCobroPlataformas(AuditTestCase):
	properties = (PROP,)

	def setUp(self):
		super().setUp()
		make_property(PROP, free_cancel_days=5)
		self.rt = make_room_type(PROP, "CPL", base_price=1000)

	def reserva(self, llega: int, **campos):
		return make_reservation(PROP, self.rt, str(add_days(nowdate(), llega)), str(add_days(nowdate(), llega + 2)),
		                        **campos).name

	def test_una_reserva_de_plataforma_vieja_pasa_a_la_cobra_el_hotel(self):
		vieja = self.reserva(30, source="OTA", channel="Booking.com", ota_ref="UT-CPL-1")
		con_dato = self.reserva(40, source="OTA", channel="Expedia", ota_ref="UT-CPL-2", ota_collect="OTA")
		directa = self.reserva(50)
		airbnb = self.reserva(60, source="OTA", channel="Airbnb", ota_ref="UT-CPL-3")
		# R5: la de Airbnb que ya está en casa el día del despliegue
		en_casa = make_reservation(PROP, self.rt, nowdate(), str(add_days(nowdate(), 2)), status="Checked In",
		                           room=make_room(PROP, self.rt, "CPL1"), source="OTA", channel="Airbnb",
		                           ota_ref="UT-CPL-4").name
		self.assertEqual(self.lineas(en_casa), [])
		patch.execute()
		patch.execute()

		fila = frappe.db.get_value("Reservation", vieja, ["ota_collect", "is_pay_at_hotel", "ota_charge_from",
		                                                  "ota_free_cancel_until", "creation", "check_in_date"],
		                           as_dict=True)
		self.assertEqual((fila.ota_collect, fila.is_pay_at_hotel), ("Hotel", 1))
		self.assertEqual(getdate(fila.ota_charge_from), getdate(fila.creation))
		self.assertEqual(getdate(fila.ota_free_cancel_until), getdate(add_days(fila.check_in_date, -5)))
		self.assertEqual(frappe.db.get_value("Reservation", con_dato, ["ota_collect", "is_pay_at_hotel"]),
		                 ("OTA", 0))
		self.assertFalse(frappe.db.get_value("Reservation", directa, "ota_collect"))
		# D-S4-4: la de Airbnb la cobró la plataforma; nunca «por cobrar»
		self.assertEqual(frappe.db.get_value("Reservation", airbnb, ["ota_collect", "is_pay_at_hotel", "ota_charge_from"]),
		                 ("OTA", 0, None))
		# en casa: su línea `OTA Prepaid` por el total, y la segunda pasada no la duplica
		total = frappe.db.get_value("Reservation", en_casa, "amount_after_tax")
		self.assertGreater(total, 0)
		self.assertEqual(self.lineas(en_casa), [round(float(total), 2)])

	def test_la_de_airbnb_en_casa_con_un_pago_en_el_folio_no_recibe_la_linea(self):
		# recepción ya apuntó a mano el pago de Airbnb: la línea sería el doble (REVISION-lote18 nota 2)
		from kamra import api
		from kamra.folio import open_folio
		en_casa = make_reservation(PROP, self.rt, nowdate(), str(add_days(nowdate(), 2)), status="Checked In",
		                           room=make_room(PROP, self.rt, "CPL2"), source="OTA", channel="Airbnb",
		                           ota_ref="UT-CPL-5").name
		folio = open_folio(frappe.get_doc("Reservation", en_casa))
		api.add_folio_payment(folio, "Cash", 500, reference="UT CPL a mano")
		patch.execute()
		self.assertEqual(self.lineas(en_casa), [])
		self.assertEqual(frappe.db.get_value("Reservation", en_casa, "ota_collect"), "OTA")

	def lineas(self, reserva):
		return [float(p.amount) for p in frappe.get_all("Folio Payment", fields=["amount"],
		                                                filters={"parent": guest_folio(reserva), "mode": "OTA Prepaid"})]
