"""El vale de la reserva web (HR-377), en bench: el IVA del residente sobre el precio con descuento, el uso que vuelve
si la reserva caduca sin pagar y el código en mayúsculas (kamra_uy/booking/voucher.py, fiscal/connector.py)."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import frappe
from frappe.utils import add_to_date, getdate, now_datetime, nowdate
from kamra.reservation_state import expire_holds
from kamra.tests.audit_fixtures import AuditTestCase, make_property, make_room, make_room_type

from kamra_uy.booking import residence

PROP = "UT Vale UY"


def _enero() -> date:
	"""El próximo 20 de enero que quede por delante: sus 3 noches, de temporada (IVA del 10 % al residente)."""
	today = getdate(nowdate())
	return next(date(y, 1, 20) for y in (today.year, today.year + 1) if date(y, 1, 20) > today + timedelta(days=1))


class TestVoucher(AuditTestCase):
	properties = (PROP,)

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		residence.ensure_fields()

	def setUp(self):
		super().setUp()
		make_property(PROP, country="Uruguay", currency="USD", uy_iva_room_pct=10, uy_iva_standard_pct=22,
		              booking_engine_enabled=1, booking_mode="Instant", booking_payment_mode="Full online",
		              hold_minutes=30, advance_percent=0, registration_fee=0, cleaning_fee=0, booking_language="")
		self.rt = make_room_type(PROP, "VAL", base_price=160, adults_capacity=2)
		make_room(PROP, self.rt, "V101")
		form = frappe.local.form_dict
		self.addCleanup(setattr, frappe.local, "form_dict", form)
		frappe.local.form_dict = frappe._dict()
		self.voucher = self.make_voucher("prueba10 ")

	def make_voucher(self, code: str) -> str:
		return frappe.get_doc({"doctype": "Discount Voucher", "property": PROP, "voucher_code": code,
		                       "discount_type": "Percent", "value": 10, "min_nights": 1, "max_uses": 0}).insert(
			ignore_permissions=True).name

	def book(self) -> dict:
		check_in = _enero()
		return residence.book(property=PROP, room_type=self.rt, check_in_date=str(check_in),
		                      check_out_date=str(check_in + timedelta(days=3)), guest_name="UT Persona Vale",
		                      phone=f"+5989965{uuid.uuid4().int % 10000:04d}", adults=2, uy_resident="1",
		                      voucher_code="PRUEBA10")

	def used(self) -> int:
		return int(frappe.db.get_value("Discount Voucher", self.voucher, "times_used") or 0)

	def test_el_residente_paga_el_iva_sobre_el_precio_con_descuento(self):
		out = self.book()
		res = frappe.get_doc("Reservation", out["reservation"])
		self.assertEqual(res.voucher, self.voucher)
		self.assertAlmostEqual(float(res.discount_amount), 48.0, places=2)
		self.assertAlmostEqual(float(res.amount_before_tax), 432.0, places=2)
		self.assertAlmostEqual(float(res.tax_amount), 43.20, places=2)
		self.assertAlmostEqual(float(res.amount_after_tax), 475.20, places=2)
		self.assertAlmostEqual(float(res.advance_due), 475.20, places=2)
		self.assertEqual(self.used(), 1)
		iva = residence.booking_status(out["status_token"])["uy_iva"]
		self.assertEqual((iva["discount_amount"], iva["voucher_code"]), (48.0, "PRUEBA10"))
		self.assertEqual(iva["amount_after_tax"], 475.20)

	def test_la_reserva_que_caduca_sin_pagar_devuelve_el_uso(self):
		out = self.book()
		self.assertEqual(self.used(), 1)
		frappe.db.set_value("Reservation", out["reservation"],
		                    {"status": "Pending Payment", "hold_expires_on": add_to_date(now_datetime(), minutes=-5)})
		expire_holds()
		self.assertEqual(frappe.db.get_value("Reservation", out["reservation"], "status"), "Cancelled")
		self.assertEqual(self.used(), 0)

	def test_el_codigo_se_guarda_en_mayusculas(self):
		name = self.make_voucher("  prueba5 ")
		self.assertEqual(frappe.db.get_value("Discount Voucher", name, "voucher_code"), "PRUEBA5")
		self.assertEqual(frappe.db.get_value("Discount Voucher", self.voucher, "voucher_code"), "PRUEBA10")
