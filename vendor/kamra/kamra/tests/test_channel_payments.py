"""Who collects a channel booking and from when (HR-380): the pure rules.

A wrong answer here marks a booking paid that is not, or the reverse, so
each rule that decides it has its case - with the one real Booking.com
revision measured on 27-11 (penalty from 2026-12-22T03:00:00 UTC, a
Montevideo hotel, arrival 27-12, 5 free-cancellation days)."""

from datetime import date

from frappe.tests import UnitTestCase

from kamra import channel_payments as cp
from kamra.channels import channex


class CollectorTestCase(UnitTestCase):
	def test_who_collects(self):
		self.assertEqual(cp.collector_of("ota", "bank_transfer"), "OTA")
		self.assertEqual(cp.collector_of("ota", "credit_card"), "Virtual card")
		self.assertEqual(cp.collector_of("ota", None), "Virtual card")
		self.assertEqual(cp.collector_of("property", "credit_card"), "Hotel")
		self.assertEqual(cp.collector_of(None, None), "Hotel")
		self.assertEqual(cp.collector_of(None, "bank_transfer"), "Hotel")
		# D-S4-4: Airbnb always collects itself, whatever the revision says
		self.assertEqual(cp.collector_of(None, None, "AirBNB"), "OTA")
		self.assertEqual(cp.collector_of("property", "credit_card", "Airbnb"), "OTA")


class ChargeFromTestCase(UnitTestCase):
	def test_the_hotel_charges_the_day_it_came_in(self):
		# D-HR-95: as soon as possible, not when the free cancellation ends
		self.assertEqual(cp.charge_from_of("Hotel", "2026-12-27", "2026-11-27"), date(2026, 11, 27))
		self.assertEqual(cp.charge_from_of("Virtual card", "2026-12-27", "2026-11-27"), date(2026, 12, 27))
		self.assertIsNone(cp.charge_from_of("OTA", "2026-12-27", "2026-11-27"))


class FreeCancelUntilTestCase(UnitTestCase):
	def test_the_real_revision(self):
		self.assertEqual(
			cp.free_cancel_until_of("2026-12-22T03:00:00", "2026-12-27", 5, "2026-11-27",
			                        "America/Montevideo"),
			date(2026, 12, 21))

	def test_without_a_penalty_kamra_s_own_window(self):
		self.assertEqual(
			cp.free_cancel_until_of(None, "2026-12-27", 5, "2026-11-27", "America/Montevideo"),
			date(2026, 12, 22))

	def test_non_refundable(self):
		# charged from the instant it was booked: never free to cancel
		self.assertIsNone(
			cp.free_cancel_until_of("2026-11-27T02:06:44", "2026-12-27", 5, "2026-11-27",
			                        "America/Montevideo"))

	def test_the_revision_carries_who_collects_and_the_penalty(self):
		rooms = [{"checkin_date": "2026-12-27", "checkout_date": "2027-01-01", "room_type_id": "rt",
		          "rate_plan_id": "rp", "amount": "1323.00", "days": {"2026-12-27": "264.60"},
		          "occupancy": {"adults": 2},
		          "meta": {"cancel_penalties": [
			          {"amount": "0.00", "currency": "USD", "from": "2026-11-27T02:06:44"},
			          {"amount": "1323.00", "currency": "USD", "from": "2026-12-22T03:00:00"}]}}]
		(e,) = channex.revision_events({
			"id": "rev", "ota_reservation_code": "5512", "ota_name": "Booking.com", "status": "new",
			"amount": "1323.00", "currency": "USD", "payment_collect": "property",
			"payment_type": "credit_card", "rooms": rooms})
		self.assertEqual((e["payment_collect"], e["payment_type"], e["penalty_from"]),
		                 ("property", "credit_card", "2026-12-22T03:00:00"))
