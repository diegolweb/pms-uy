"""HR-332 (D-HR-65, H3: «se espera 120 minutos, al igual que una reserva online»): la reserva por teléfono sin pagar vence
sola a los 120 minutos, como la de la web.

kamra_uy enciende `hold_unpaid_desk_bookings` en las propiedades del sitio (hooks.py, kamra_setting_defaults). En una
propiedad del hotel (cobro completo al reservar: «Full online», deposit_pct 100, hold_minutes 120), la reserva que hace
recepción (kamra.api.create_booking) nace en «Pago pendiente» con su retención, y el trabajo de Kamra que suelta las de la
web (kamra.reservation_state.expire_holds, cada 15 minutos) la cancela pasados los 120 minutos. La que se paga (el aviso
del pago de Mercado Pago: kamra.payments.settle_payment_link) queda confirmada y no vence.

Lo corre `bench run-tests --module kamra_uy.booking.tests.test_desk_hold`.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import get_datetime, now_datetime
from kamra import api
from kamra.folio import open_folio
from kamra.payments import settle_payment_link
from kamra.reservation_state import expire_holds
from kamra.tests.audit_fixtures import AuditTestCase, d, ensure_user, make_room, make_room_type

from kamra_uy.payments.tests.fixtures import make_uy_property

PROP = "UT Recepción Retención"
DESK = "recepcion-retencion-ut@example.com"


class TestDeskHold(AuditTestCase):
	properties = (PROP,)

	def setUp(self):
		super().setUp()
		make_uy_property(PROP)
		frappe.db.set_value("Property", PROP, {"booking_payment_mode": "Full online", "deposit_pct": 100,
		                                       "hold_minutes": 120})
		frappe.clear_document_cache("Property", PROP)
		self.rt = make_room_type(PROP, "RET", base_price=100)
		make_room(PROP, self.rt, "T101")
		make_room(PROP, self.rt, "T102")
		ensure_user(DESK, ["Front Desk"])

	def phone_booking(self, n: int) -> str:
		frappe.set_user(DESK)  # nosemgrep: frappe-setuser -- test fixture
		try:
			return api.create_booking(property=PROP, room_type=self.rt, check_in_date=d(10), check_out_date=d(12),
			                          guest_name=f"UT Teléfono Retención {n}", phone=f"+5989933{n:04d}", adults=2,
			                          tax_resident="0")["reservation"]
		finally:
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture

	def test_la_reserva_de_recepcion_sin_pagar_vence_a_los_120_minutos_y_la_pagada_no(self):
		booked = now_datetime()
		unpaid, paid = self.phone_booking(1), self.phone_booking(2)
		for name in (unpaid, paid):
			status, expires = frappe.db.get_value("Reservation", name, ["status", "hold_expires_on"])
			self.assertEqual(status, "Pending Payment")
			self.assertAlmostEqual((get_datetime(expires) - booked).total_seconds() / 60, 120, delta=2)

		folio = open_folio(frappe.get_doc("Reservation", paid))
		total = float(frappe.db.get_value("Reservation", paid, "amount_after_tax"))
		self.assertTrue(settle_payment_link(folio, "ut-link-retencion", total, reference="ut-pago-retencion"))
		self.assertEqual(frappe.db.get_value("Reservation", paid, "status"), "Confirmed")

		def later(minutes: int):
			return patch("frappe.utils.now_datetime", return_value=booked + timedelta(minutes=minutes))

		with later(118):
			expire_holds()
		self.assertEqual(frappe.db.get_value("Reservation", unpaid, "status"), "Pending Payment")
		with later(122):
			expire_holds()
		self.assertEqual(frappe.db.get_value("Reservation", unpaid, "status"), "Cancelled")
		self.assertEqual(frappe.db.get_value("Reservation", paid, "status"), "Confirmed")
