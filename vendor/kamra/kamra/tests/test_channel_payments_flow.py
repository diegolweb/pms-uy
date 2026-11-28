"""Who collects a channel booking, end to end (HR-380).

Channex revisions go through revision_events and _apply_event, as a
webhook delivery does, and the money is read back where the desk reads it:
the stay balance, the deposit, the drawer's notice, the manager's list on
Today, the no-show fee, a channel cancel and the folio refund. Each case is
one way a booking would show paid when it is not, or the reverse, or a
refund would be lost."""

from __future__ import annotations

import frappe
from frappe.utils import add_days, getdate, nowdate

from kamra import api, cashier, checkout, ledger, no_show
from kamra import channel_manager as cm
from kamra import channel_payments as cp
from kamra.channels import channex
from kamra.folio import post_remaining_nights
from kamra.tests.audit_fixtures import (AuditTestCase, ensure_user, guest_folio, make_property, make_room,
                                        make_room_type)
from kamra.tests.test_billing_approvals_split import set_setting

PROP = "UT HR380 Cobros"


class ChannelPaymentsFlowTestCase(AuditTestCase):
	properties = (PROP,)

	def setUp(self):
		super().setUp()
		# the hotel's own policy: free until 5 days out, then the first night
		make_property(PROP, free_cancel_days=5, cancellation_fee="First Night",
		              no_show_charge="First Night", waive_no_show_fee_for_ota=1)
		self.currency = frappe.db.get_value("Property", PROP, "currency")
		self.rt = make_room_type(PROP, "H380", base_price=1000)
		self.room = make_room(PROP, self.rt, "380")
		self.room2 = make_room(PROP, self.rt, "381")
		self.conn = frappe.get_doc({"doctype": "Channel Manager Connection", "property": PROP,
			"provider": "AioSell", "active": 1, "external_property_id": "hr380"}).insert(
			ignore_permissions=True)
		frappe.get_doc({"doctype": "Channel Room Mapping", "connection": self.conn.name,
			"room_type": self.rt, "external_room_id": "h380"}).insert(ignore_permissions=True)
		self.today = getdate(nowdate())

	# --- helpers -------------------------------------------------------------

	def book(self, collect, ptype, arrive_in, nights=2, rate=500, penalty_in=None):
		"""A Booking.com revision through the channel path; the reservation."""
		ref = "H380-" + frappe.generate_hash(length=8)
		ci = add_days(self.today, arrive_in)
		days = {str(add_days(ci, i)): f"{rate:.2f}" for i in range(nights)}
		penalties = [{"amount": "0.00", "from": f"{self.today}T00:00:00"}]
		if penalty_in is not None:
			penalties.append({"amount": f"{rate * nights:.2f}",
			                  "from": f"{add_days(self.today, penalty_in)}T03:00:00"})
		rev = {"id": "rev-" + ref, "ota_reservation_code": ref, "ota_name": "Booking.com",
			"status": "new", "amount": f"{rate * nights:.2f}", "currency": self.currency,
			"payment_collect": collect, "payment_type": ptype,
			"customer": {"name": "UT", "surname": "Cobro"},
			"rooms": [{"checkin_date": str(ci), "checkout_date": str(add_days(ci, nights)),
			           "room_type_id": "h380", "rate_plan_id": "rp", "amount": f"{rate * nights:.2f}",
			           "days": days, "occupancy": {"adults": 2, "children": 0},
			           "meta": {"cancel_penalties": penalties}}]}
		for e in channex.revision_events(rev):
			out = cm._apply_event(self.conn, e)
			self.assertEqual(out["result"], "booked", out)
		return frappe.get_doc("Reservation", {"property": PROP, "ota_ref": ref})

	def cancel(self, res):
		(e,) = channex.revision_events({"id": "cxl-" + res.ota_ref, "status": "cancelled",
		                                "ota_reservation_code": res.ota_ref, "ota_name": "Booking.com"})
		cm._apply_event(self.conn, e)
		return frappe.get_doc("Reservation", res.name)

	def collection(self, name):
		res = frappe.get_doc("Reservation", name)
		return cp.collection(res, api._stay_balance(name))

	def listed(self, name):
		return next((r for r in cp.to_collect(PROP)["rows"] if r["name"] == name), None)

	def vouchers(self):
		return frappe.db.count("Discount Voucher", {"property": PROP})

	def deposit_net(self, folio):
		return float(frappe.db.sql(
			"SELECT COALESCE(SUM(credit - debit), 0) FROM `tabFolio Ledger Entry` "
			"WHERE folio = %s AND ledger = 'Deposit'", folio)[0][0])

	# --- a. the platform collected -------------------------------------------

	def test_a_platform_collected_booking_is_paid_and_not_listed(self):
		res = self.book("ota", "bank_transfer", 30)
		self.assertEqual((res.ota_collect, res.is_pay_at_hotel), ("OTA", 0))
		self.assertIsNone(res.ota_charge_from)
		money = api._stay_balance(res.name)
		self.assertEqual((money["paid"], money["due"], money["source"]), (1000.0, 0.0, "ota"))
		self.assertEqual(api._deposit_state(res)["due"], 0)
		self.assertEqual(self.collection(res.name)["state"], "ota_paid")
		self.assertIsNone(self.listed(res.name))

	# --- b. the hotel collects, at once (D-HR-95) ----------------------------

	def test_b_a_hotel_collected_booking_is_due_from_the_day_it_came_in(self):
		res = self.book("property", "credit_card", 30, penalty_in=25)
		self.assertEqual((res.ota_collect, res.is_pay_at_hotel), ("Hotel", 1))
		self.assertEqual(getdate(res.ota_charge_from), cp._local_today(PROP))
		self.assertEqual(
			getdate(res.ota_free_cancel_until),
			cp.free_cancel_until_of(f"{add_days(self.today, 25)}T03:00:00", res.check_in_date, 5,
			                        cp._local_today(PROP), cp._property_tz(PROP)))
		self.assertEqual(api._stay_balance(res.name)["due"], 1000.0)
		self.assertEqual(self.collection(res.name)["state"], "to_collect")
		self.assertEqual(self.listed(res.name)["state"], "now")
		api.record_advance(res.name, 1000, "Card", reference="UT HR380")
		self.assertEqual(self.collection(res.name)["state"], "collected")
		self.assertIsNone(self.listed(res.name))

	# --- c. the platform's payment on the folio at check-in (R5) ---------------

	def tills_on(self):
		set_setting(PROP, "use_cashier_tills", 1)

	def ota_lines(self, name):
		return frappe.get_all("Folio Payment", filters={"parent": guest_folio(name), "mode": "OTA Prepaid"},
		                      fields=["name", "amount", "payment_kind", "reference"])

	def test_c_check_in_posts_the_platform_payment_without_a_till(self):
		self.tills_on()
		res = self.book("ota", "bank_transfer", 0)
		api.record_advance(res.name, 100, "Cash", reference="UT HR380")
		# the night auditor checks in: Reservation check_in_out, no Folio bill, no till open
		night = ensure_user("ut.hr380.night@test.local", ["Night Auditor"])
		self.assertFalse(frappe.db.exists("Cashier Session", {"property": PROP, "user": night, "status": "Open"}))
		till_rows = frappe.db.count("Cashier Transaction", {"property": PROP})
		frappe.set_user(night)  # nosemgrep: frappe-setuser -- the role under test
		api.check_in(res.name, room=self.room)
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- back to the fixture user
		# c1: in, with one OTA Prepaid line for the total less the advance
		self.assertEqual(frappe.db.get_value("Reservation", res.name, "status"), "Checked In")
		folio = guest_folio(res.name)
		lines = self.ota_lines(res.name)
		self.assertEqual([(r.amount, r.payment_kind) for r in lines], [(900.0, "Payment")])
		self.assertEqual(lines[0].reference, f"Booking.com {res.ota_ref}")
		# no till saw it: no money came into a drawer
		self.assertEqual(frappe.db.count("Cashier Transaction", {"property": PROP}), till_rows)
		self.assertFalse(frappe.db.exists("Cashier Transaction", {"folio": folio, "mode": "OTA Prepaid"}))
		guest = frappe.get_all("Folio Ledger Entry", fields=["credit", "debit"],
		                       filters={"folio": folio, "ledger": "Guest", "source_doctype": "Folio Payment",
		                                "source_name": lines[0].name})
		self.assertEqual([(float(e.credit), float(e.debit)) for e in guest], [(900.0, 0.0)])
		self.assertNotIn(folio, [f["folio"] for f in ledger.folio_reconciliation(PROP, limit=500)["folios"]])
		# c2: a second post finds it by its key
		res.reload()
		self.assertEqual(cp.post_platform_payment(res), lines[0].name)
		self.assertEqual(len(self.ota_lines(res.name)), 1)
		# c3: the nights posted, the guest owes nothing at check-out and is owed nothing
		self.assertEqual(post_remaining_nights(res), 2)
		self.assertAlmostEqual(float(frappe.db.get_value("Folio", folio, "grand_total")),
		                       float(res.amount_after_tax), delta=0.01)
		pos = checkout.settlement_position(res.name)
		self.assertEqual((pos["owed"], pos["refund_due"]), (0.0, 0.0))

	def test_c4_a_hotel_collected_check_in_leaves_no_platform_line(self):
		self.tills_on()
		res = self.book("property", "credit_card", 0)
		desk = ensure_user("ut.hr380.desk@test.local", ["Front Desk"])
		frappe.set_user(desk)  # nosemgrep: frappe-setuser -- the role under test
		cashier.open_session(PROP, 0)
		api.check_in(res.name, room=self.room2)
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- back to the fixture user
		self.assertEqual(frappe.db.get_value("Reservation", res.name, "status"), "Checked In")
		self.assertEqual(self.ota_lines(res.name), [])
		self.assertIsNone(cp.post_platform_payment(frappe.get_doc("Reservation", res.name)))

	# --- c5/c6. undoing the check-in of a booking the platform collected -------

	def same_day(self, name):
		"""The blocker wants the check-in on the business date (as test_reverse_check_in does)."""
		from kamra.business_date import get_business_date
		bd = str(get_business_date(PROP))
		at = frappe.db.get_value("Reservation", name, "checked_in_at")
		if not at or str(getdate(at)) != bd:
			frappe.db.set_value("Reservation", name, "checked_in_at", f"{bd} 10:00:00")

	def guest_entries(self, folio, row):
		return frappe.get_all("Folio Ledger Entry", fields=["name", "credit", "debit", "reversal_of"],
		                      filters={"folio": folio, "ledger": "Guest", "source_doctype": "Folio Payment",
		                               "source_name": row})

	def guest_net(self, folio):
		return round(float(frappe.db.sql(
			"""SELECT COALESCE(SUM(debit - credit), 0) FROM `tabFolio Ledger Entry`
			   WHERE folio = %s AND ledger = 'Guest'""", folio)[0][0] or 0), 2)

	def unreconciled(self):
		return [f["folio"] for f in ledger.folio_reconciliation(PROP, limit=500)["folios"]]

	def test_c5_undo_and_redo_the_check_in_of_a_platform_collected_booking(self):
		res = self.book("ota", "bank_transfer", 0)
		amount = round(float(res.amount_after_tax), 2)
		# 1. in, with its OTA Prepaid line, and the undo is offered
		api.check_in(res.name, room=self.room)
		self.same_day(res.name)
		folio = guest_folio(res.name)
		first = self.ota_lines(res.name)
		self.assertEqual([(r.amount, r.payment_kind) for r in first], [(amount, "Payment")])
		self.assertIsNone(api._reverse_check_in_blocker(frappe.get_doc("Reservation", res.name)))
		# 2. undone: the line is off the bill and its credit is offset on the Guest ledger
		api.reverse_check_in(res.name, "Wrong guest")
		self.assertEqual(frappe.db.get_value("Reservation", res.name, "status"), "Confirmed")
		self.assertEqual(self.ota_lines(res.name), [])
		entries = self.guest_entries(folio, first[0].name)
		self.assertEqual(sorted(((float(e.credit), float(e.debit)) for e in entries), reverse=True),
		                 [(amount, 0.0), (0.0, amount)])
		credit = next(e for e in entries if float(e.credit))
		debit = next(e for e in entries if float(e.debit))
		self.assertFalse(credit.reversal_of)
		self.assertEqual(debit.reversal_of, credit.name)
		self.assertEqual(float(frappe.db.get_value("Folio", folio, "balance")), 0.0)
		self.assertNotIn(folio, self.unreconciled())
		self.assertTrue(frappe.db.exists("Agent Action Log", {
			"action_type": "payment_reversed", "reference_doctype": "Folio", "reference_name": folio}))
		# 3. in again: the line comes back once, a new one
		frappe.db.set_value("Room", self.room, "housekeeping_status", "Clean")
		api.check_in(res.name, room=self.room)
		folios = frappe.get_all("Folio", filters={"reservation": res.name}, pluck="name")
		lines = frappe.get_all("Folio Payment", filters={"parent": ("in", folios), "mode": "OTA Prepaid"},
		                       fields=["name", "parent", "amount"])
		self.assertEqual([r.amount for r in lines], [amount])
		self.assertNotEqual(lines[0].name, first[0].name)
		self.assertEqual([(float(e.credit), float(e.debit)) for e in self.guest_entries(lines[0].parent,
		                                                                               lines[0].name)],
		                 [(amount, 0.0)])
		self.assertEqual(self.guest_net(lines[0].parent), -amount)
		self.assertEqual(float(frappe.db.get_value("Folio", lines[0].parent, "balance")), -amount)
		self.assertNotIn(lines[0].parent, self.unreconciled())

	def test_c6_another_payment_still_blocks_the_undo(self):
		res = self.book("ota", "bank_transfer", 0)
		api.record_advance(res.name, 100, "Cash", reference="UT HR380")
		api.check_in(res.name, room=self.room)
		self.same_day(res.name)
		with self.assertRaisesRegex(frappe.ValidationError, "charge or payment is on the bill"):
			api.reverse_check_in(res.name, "Wrong guest")
		self.assertEqual(frappe.db.get_value("Reservation", res.name, "status"), "Checked In")
		self.assertEqual([r.payment_kind for r in self.ota_lines(res.name)], ["Payment"])

	# --- m. a channel modification with the guest in house ---------------------

	def modify(self, res, nights, rate, rev_id=None):
		"""The booking's revision again, modified on the channel: same arrival, new length; the revision id."""
		ref = res.ota_ref
		ci = getdate(res.check_in_date)
		rev_id = rev_id or "rev-" + frappe.generate_hash(length=8)
		days = {str(add_days(ci, i)): f"{rate:.2f}" for i in range(nights)}
		rev = {"id": rev_id, "ota_reservation_code": ref, "ota_name": "Booking.com",
			"status": "modified", "amount": f"{rate * nights:.2f}", "currency": self.currency,
			"payment_collect": "ota", "payment_type": "bank_transfer",
			"customer": {"name": "UT", "surname": "Cobro"},
			"rooms": [{"checkin_date": str(ci), "checkout_date": str(add_days(ci, nights)),
			           "room_type_id": "h380", "rate_plan_id": "rp", "amount": f"{rate * nights:.2f}",
			           "days": days, "occupancy": {"adults": 2, "children": 0},
			           "meta": {"cancel_penalties": [{"amount": "0.00", "from": f"{self.today}T00:00:00"}]}}]}
		for e in channex.revision_events(rev):
			out = cm._apply_event(self.conn, e)
			self.assertEqual(out["result"], "modified", out)
		return rev_id

	def in_house(self, nights):
		res = self.book("ota", "bank_transfer", 0, nights=nights, rate=500)
		api.check_in(res.name, room=self.room)
		self.same_day(res.name)
		return frappe.get_doc("Reservation", res.name)

	def all_ota_lines(self, name):
		folios = frappe.get_all("Folio", filters={"reservation": name}, pluck="name")
		return frappe.get_all("Folio Payment", filters={"parent": ("in", folios), "mode": "OTA Prepaid"},
		                      fields=["name", "parent", "amount", "idempotency_key"], order_by="idx asc")

	def settles_even(self, name):
		pos = checkout.settlement_position(name)
		self.assertEqual((pos["owed"], pos["refund_due"]), (0.0, 0.0))

	def test_m1_a_longer_stay_adds_the_platform_difference(self):
		res = self.in_house(2)
		folio = guest_folio(res.name)
		rev = self.modify(res, 3, 500)
		lines = self.all_ota_lines(res.name)
		self.assertEqual([r.amount for r in lines], [1000.0, 500.0])
		self.assertEqual(lines[1].idempotency_key, f"{cp.platform_payment_key(res.name)}:{rev}")
		self.assertEqual(self.guest_net(folio), -1500.0)
		self.assertEqual(float(frappe.db.get_value("Folio", folio, "balance")), -1500.0)
		self.assertNotIn(folio, self.unreconciled())
		self.assertEqual(post_remaining_nights(frappe.get_doc("Reservation", res.name)), 3)
		self.settles_even(res.name)
		# the same revision again adds nothing
		self.modify(frappe.get_doc("Reservation", res.name), 3, 500, rev_id=rev)
		self.assertEqual([r.amount for r in self.all_ota_lines(res.name)], [1000.0, 500.0])

	def test_m2_a_shorter_stay_takes_the_difference_off_without_a_refund(self):
		res = self.in_house(3)
		folio = guest_folio(res.name)
		first = self.all_ota_lines(res.name)
		self.modify(res, 2, 500)
		lines = self.all_ota_lines(res.name)
		self.assertEqual([(r.name, r.amount) for r in lines], [(first[0].name, 1000.0)])
		self.assertFalse(frappe.db.exists("Folio Payment", {"parent": folio, "payment_kind": "Refund"}))
		entries = self.guest_entries(folio, first[0].name)
		self.assertEqual(sorted(((float(e.credit), float(e.debit)) for e in entries), reverse=True),
		                 [(1500.0, 0.0), (0.0, 500.0)])
		credit = next(e for e in entries if float(e.credit))
		debit = next(e for e in entries if float(e.debit))
		self.assertFalse(credit.reversal_of)
		self.assertEqual(debit.reversal_of, credit.name)
		self.assertEqual(float(frappe.db.get_value("Folio", folio, "balance")), self.guest_net(folio))
		self.assertNotIn(folio, self.unreconciled())
		self.assertEqual(post_remaining_nights(frappe.get_doc("Reservation", res.name)), 2)
		self.settles_even(res.name)

	def test_m3_undo_after_a_modification_takes_every_platform_line_off(self):
		res = self.in_house(2)
		folio = guest_folio(res.name)
		self.modify(res, 3, 500)
		self.assertEqual(len(self.all_ota_lines(res.name)), 2)
		api.reverse_check_in(res.name, "Wrong guest")
		self.assertEqual(frappe.db.get_value("Reservation", res.name, "status"), "Confirmed")
		self.assertEqual(self.all_ota_lines(res.name), [])
		self.assertEqual(self.guest_net(folio), 0.0)
		self.assertNotIn(folio, self.unreconciled())

	def test_m4_a_stay_with_no_platform_line_is_not_adjusted(self):
		# in house from before R5: no OTA Prepaid line, the desk took the money by hand
		res = self.book("ota", "bank_transfer", 0, nights=2, rate=500)
		frappe.db.set_value("Reservation", res.name, "ota_collect", cp.HOTEL)
		api.check_in(res.name, room=self.room)
		frappe.db.set_value("Reservation", res.name, "ota_collect", cp.PLATFORM)
		res = frappe.get_doc("Reservation", res.name)
		self.assertEqual(self.all_ota_lines(res.name), [])
		from kamra.folio import open_folio
		folio = open_folio(res)
		api.add_folio_payment(folio, "Cash", 1000, reference="UT HR380 a mano")
		logged = frappe.db.count("Error Log", {"method": "platform payment not adjusted"})
		self.modify(res, 3, 500)
		self.assertEqual(frappe.db.count("Error Log", {"method": "platform payment not adjusted"}), logged + 1)
		self.assertEqual(self.all_ota_lines(res.name), [])
		pays = frappe.get_all("Folio Payment", filters={"parent": folio}, fields=["mode", "amount"])
		self.assertEqual([(p.mode, float(p.amount)) for p in pays], [("Cash", 1000.0)])

	def test_m5_an_advance_that_covered_the_stay_still_gets_the_longer_part(self):
		# the advance paid the whole stay: check-in posts no platform line, and a longer stay posts the difference
		res = self.book("ota", "bank_transfer", 0, nights=2, rate=500)
		api.record_advance(res.name, float(res.amount_after_tax), "Cash", reference="UT HR380 anticipo")
		api.check_in(res.name, room=self.room)
		self.assertEqual(self.all_ota_lines(res.name), [])
		rev = self.modify(frappe.get_doc("Reservation", res.name), 3, 500)
		lines = self.all_ota_lines(res.name)
		self.assertEqual([r.amount for r in lines], [500.0])
		self.assertEqual(lines[0].idempotency_key, f"{cp.platform_payment_key(res.name)}:{rev}")
		self.assertNotIn(lines[0].parent, self.unreconciled())
		self.assertEqual(post_remaining_nights(frappe.get_doc("Reservation", res.name)), 3)
		self.settles_even(res.name)

	# --- d. the no-show fee ---------------------------------------------------

	def test_d_a_no_show_the_hotel_collects_is_charged(self):
		pol = no_show.policy(PROP)
		res = frappe._dict(source="OTA", ota_ref="X1", channel="Booking.com")
		for collector, fee in (("Hotel", "charge"), ("Virtual card", "charge"), ("OTA", "waived"),
		                       ("", "waived")):
			with self.subTest(collector=collector):
				self.assertEqual(no_show.fee_decision(frappe._dict(res, ota_collect=collector), pol).fee, fee)

	# --- e. a cancel from Channex ---------------------------------------------

	def test_e_a_channel_cancel_follows_who_collects(self):
		before = self.vouchers()
		# the platform collected: no fee, though the hotel's own window would charge (3 < 5 days)
		platform = self.cancel(self.book("ota", "bank_transfer", 3))
		self.assertEqual((platform.status, float(platform.cancellation_fee)), ("Cancelled", 0.0))
		# the measured 22-12 case: 5 days out is free in Kamra's window, the channel already charges
		late = self.book("property", "credit_card", 5)
		late.db_set("ota_free_cancel_until", add_days(self.today, -1), update_modified=False)
		late = self.cancel(late)
		self.assertGreater(float(late.cancellation_fee), 0)
		# still free on the channel's policy today, though inside the hotel's window
		in_time = self.book("property", "credit_card", 3)
		in_time.db_set("ota_free_cancel_until", self.today, update_modified=False)
		in_time = self.cancel(in_time)
		self.assertEqual((in_time.status, float(in_time.cancellation_fee)), ("Cancelled", 0.0))
		self.assertEqual(self.vouchers(), before)

	# --- f. a paid booking cancelled in time is refunded from the folio -------

	def test_f_a_cancel_in_time_is_refunded_from_the_folio(self):
		res = self.book("property", "credit_card", 30, penalty_in=25)
		api.record_advance(res.name, 1000, "Card", reference="UT HR380")
		before = self.vouchers()
		res = self.cancel(res)
		self.assertEqual((res.status, float(res.cancellation_fee)), ("Cancelled", 0.0))
		self.assertEqual(self.vouchers(), before)
		c = self.collection(res.name)
		self.assertEqual((c["state"], c["refund"]), ("refund_due", 1000.0))
		row = self.listed(res.name)
		self.assertEqual((row["state"], row["amount"]), ("refund", 1000.0))
		folio = guest_folio(res.name)
		self.assertEqual(row["folio"], folio)
		# the folio screen's Refund box: no payment_row, the till open
		api.refund_folio_payment(folio, amount=1000, mode="Card", reason="UT cancelled in time")
		self.assertAlmostEqual(float(frappe.db.get_value("Folio", folio, "balance")), 0)
		self.assertAlmostEqual(self.deposit_net(folio), 0)
		self.assertAlmostEqual(float(frappe.db.get_value("Reservation", res.name, "advance_paid")), 0)
		self.assertIsNone(self.listed(res.name))
		self.assertEqual(self.collection(res.name)["state"], "settled")
