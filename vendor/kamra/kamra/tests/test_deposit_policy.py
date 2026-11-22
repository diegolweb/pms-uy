"""The deposit the desk asks for follows the booking page's policy.

A booking made on the public page keeps the advance it was booked with
(`Reservation.advance_due`, snapshotted by `public_api.book`), whatever the
property's payment settings say later. A desk booking on a property in
"First night" asks for one night - the stay's total divided by its nights -
outside a season, and the season's own advance % (or the whole stay, on
full-payment dates) inside one: the same terms the booking page gives
(`public_api._advance_terms`). "Advance percent" and the other modes keep
the property's deposit % (or the season's %), as before.

`_deposit_state` is what the deposit panel, the check-in screen and the
itinerary read; `deposit_payment_link` asks for its `due` when the desk
sends a link without typing an amount.

Runs with `bench run-tests --module kamra.tests.test_deposit_policy`.
"""

import frappe

from kamra import api, public_api
from kamra.tests.audit_fixtures import AuditTestCase, d, make_property, make_room, make_room_type

PROP = "UT Deposit Policy"
DESK_PHONE = "919800031154"
WEB_PHONE = "919800031155"


class DepositPolicyCase(AuditTestCase):
	properties = (PROP,)

	def setUp(self):
		super().setUp()
		frappe.local.lang = "en"
		# India: a Kamra pack that offers payment links. The deposit % (30)
		# and the booking page's advance % (20) differ on purpose, so each
		# test can tell which one a figure came from.
		make_property(PROP, country="India", booking_engine_enabled=1,
		              booking_payment_mode="First night", booking_mode="Instant",
		              hold_minutes=30, deposit_pct=30, advance_percent=20,
		              registration_fee=0, cleaning_fee=0)
		self.rt = make_room_type(PROP, "DEP", base_price=1000, adults_capacity=2)
		for number in ("D101", "D102", "D103"):
			make_room(PROP, self.rt, number)
		self.ci, self.co = d(20), d(23)  # three nights
		for name in frappe.get_all("Payment Gateway Settings", {"property": PROP}, pluck="name"):
			frappe.delete_doc("Payment Gateway Settings", name, ignore_permissions=True, force=True)

	def mode(self, mode: str):
		frappe.db.set_value("Property", PROP, "booking_payment_mode", mode)
		frappe.clear_document_cache("Property", PROP)

	def season(self, **kw):
		return frappe.get_doc({"doctype": "Season", "property": PROP,
			"season_name": "UT Peak " + frappe.generate_hash(length=6),
			"start_date": self.ci, "end_date": self.co, "adjustment_type": "Percent",
			"adjustment_value": 0, "minimum_nights": 0, "priority": 10, **kw}).insert(
			ignore_permissions=True)

	def gateway(self):
		frappe.get_doc({"doctype": "Payment Gateway Settings", "property": PROP,
		                "gateway": "Razorpay", "enabled": 1, "test_mode": 1}).insert(
			ignore_permissions=True)

	def desk_booking(self):
		"""A booking made at the desk (the booking dialog's create_booking)."""
		out = api.create_booking(PROP, self.rt, self.ci, self.co, "UT Desk Guest",
		                         phone=DESK_PHONE, adults=2)
		return frappe.get_doc("Reservation", out["reservation"])

	def web_booking(self):
		"""A booking made on the public page, with no gateway: held, unpaid."""
		out = public_api.book(property=PROP, room_type=self.rt, check_in_date=self.ci,
		                      check_out_date=self.co, guest_name="UT Web Guest",
		                      phone=WEB_PHONE, email="deposit-web@example.com", adults=2)
		return frappe.get_doc("Reservation", out["reservation"])


class TestWebBookingKeepsItsAdvance(DepositPolicyCase):
	def test_a_web_booking_expects_the_advance_it_was_booked_with(self):
		res = self.web_booking()
		stored = float(res.advance_due)
		self.assertEqual(stored, round(res.amount_after_tax / 3, 2))
		# the property changes its terms later: the guest's stay does not
		self.mode("Advance percent")
		dep = api._deposit_state(res)
		self.assertEqual(dep["expected"], stored)
		self.assertEqual(dep["due"], stored)
		self.assertTrue(dep["can_take"])

	def test_a_web_booking_under_advance_percent_expects_the_page_s_percent(self):
		self.mode("Advance percent")
		res = self.web_booking()
		self.assertEqual(float(res.advance_due), round(res.amount_after_tax * 0.2, 2))
		dep = api._deposit_state(res)
		self.assertEqual(dep["expected"], float(res.advance_due))
		self.assertEqual(dep["pct"], 20)

	def test_a_web_booking_paid_at_the_hotel_expects_no_deposit(self):
		self.mode("Pay at hotel")
		res = self.web_booking()
		self.assertEqual(float(res.advance_due or 0), 0)
		dep = api._deposit_state(res)
		self.assertEqual((dep["expected"], dep["due"], dep["pct"]), (0, 0, 0))

	def test_what_came_in_comes_off_the_stored_advance(self):
		res = self.web_booking()
		frappe.db.set_value("Reservation", res.name, "advance_paid", 100)
		res.reload()
		dep = api._deposit_state(res)
		self.assertEqual(dep["due"], round(float(res.advance_due) - 100, 2))

	def test_a_desk_booking_marked_website_without_page_terms_uses_the_desk_policy(self):
		# source picked by hand at the desk: no terms were snapshotted for it
		res = self.desk_booking()
		frappe.db.set_value("Reservation", res.name, "source", "Website")
		res.reload()
		self.assertFalse(res.payment_policy)
		self.assertEqual(api._deposit_state(res)["expected"],
		                 round(res.amount_after_tax / 3, 2))


class TestDeskBookingOnFirstNight(DepositPolicyCase):
	def test_out_of_season_it_expects_one_night(self):
		res = self.desk_booking()
		self.assertEqual(res.source, "Manual")
		total = float(res.amount_after_tax)
		self.assertGreater(total, 0)
		dep = api._deposit_state(res)
		self.assertEqual(dep["expected"], round(total / 3, 2))
		self.assertEqual(dep["due"], round(total / 3, 2))
		# one night of three is a third of the stay
		self.assertEqual(dep["pct"], round(100 * dep["expected"] / total, 2))

	def test_in_a_season_with_its_own_advance_percent_it_expects_that_percent(self):
		self.season(advance_percent=50)
		res = self.desk_booking()
		dep = api._deposit_state(res)
		self.assertEqual(dep["expected"], round(float(res.amount_after_tax) * 0.5, 2))
		self.assertEqual(dep["pct"], 50)

	def test_on_full_payment_dates_it_expects_the_whole_stay(self):
		self.season(full_payment=1)
		res = self.desk_booking()
		dep = api._deposit_state(res)
		self.assertEqual(dep["expected"], float(res.amount_after_tax))
		self.assertEqual(dep["pct"], 100)

	def test_a_one_night_stay_expects_the_night(self):
		out = api.create_booking(PROP, self.rt, self.ci, d(21), "UT Desk Guest",
		                         phone=DESK_PHONE, adults=2)
		res = frappe.get_doc("Reservation", out["reservation"])
		self.assertEqual(api._deposit_state(res)["expected"], float(res.amount_after_tax))

	def test_an_itinerary_leg_row_gets_the_same_figure_as_the_booking(self):
		# crs.get_itinerary passes only the leg's name, property, total,
		# advance paid and status: the rest is read from the booking
		res = self.desk_booking()
		row = frappe._dict(name=res.name, property=res.property,
		                   amount_after_tax=res.amount_after_tax,
		                   advance_paid=res.advance_paid, status=res.status)
		self.assertEqual(api._deposit_state(row)["expected"],
		                 api._deposit_state(res)["expected"])
		self.assertEqual(api._deposit_state(row)["expected"],
		                 round(float(res.amount_after_tax) / 3, 2))


class TestOtherModesUnchanged(DepositPolicyCase):
	def test_advance_percent_keeps_the_deposit_percent(self):
		self.mode("Advance percent")
		res = self.desk_booking()
		dep = api._deposit_state(res)
		self.assertEqual(dep["pct"], 30)
		self.assertEqual(dep["expected"], round(float(res.amount_after_tax) * 0.3, 2))

	def test_advance_percent_keeps_the_season_percent(self):
		self.mode("Advance percent")
		self.season(advance_percent=50)
		res = self.desk_booking()
		dep = api._deposit_state(res)
		self.assertEqual(dep["pct"], 50)
		self.assertEqual(dep["expected"], round(float(res.amount_after_tax) * 0.5, 2))

	def test_pay_at_hotel_keeps_the_deposit_percent(self):
		self.mode("Pay at hotel")
		res = self.desk_booking()
		dep = api._deposit_state(res)
		self.assertEqual(dep["pct"], 30)
		self.assertEqual(dep["expected"], round(float(res.amount_after_tax) * 0.3, 2))


class TestDepositPaymentLink(DepositPolicyCase):
	def test_the_desk_link_for_a_three_night_stay_asks_for_one_night(self):
		self.gateway()
		res = self.desk_booking()
		link = api.deposit_payment_link(res.name)
		expected = round(float(res.amount_after_tax) / 3, 2)
		self.assertEqual(link["amount"], expected)
		self.assertEqual(link["deposit"]["link_amount"], expected)
		self.assertIn(f"{expected:,.2f}", link["message"])

	def test_the_link_of_an_unpaid_web_booking_asks_for_its_stored_advance(self):
		res = self.web_booking()
		stored = float(res.advance_due)
		# the property's terms move on; the guest was promised one night
		self.mode("Pay at hotel")
		self.gateway()
		link = api.deposit_payment_link(res.name)
		self.assertEqual(link["amount"], stored)
		self.assertEqual(link["deposit"]["expected"], stored)

	def test_an_amount_typed_at_the_desk_still_wins(self):
		self.gateway()
		res = self.desk_booking()
		self.assertEqual(api.deposit_payment_link(res.name, amount=123)["amount"], 123)
