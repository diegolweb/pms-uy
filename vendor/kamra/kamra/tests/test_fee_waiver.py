"""Waiving a cancellation fee is the manager's call (authz.FEE_WAIVER_ROLES).

The cancel panel's "Waive the fee (logged - manager's call)" used to be
ticked by anyone at the desk. cancel_reservation now refuses waive_fee from
a Front Desk user, who can still cancel with the fee; the hotel admin waives
it. cancellation_preview says which of the two the user is.
"""

from __future__ import annotations

import frappe

from kamra import api
from kamra.tests.audit_fixtures import (
	PROP_A,
	AuditTestCase,
	d,
	ensure_user,
	make_property,
	make_room,
	make_room_type,
)

DESK = "ut-waiver-desk@kamra.local"
MANAGER = "ut-waiver-manager@kamra.local"


class TestFeeWaiver(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		# a year of notice and a first-night fee: any cancel is inside the window
		make_property(PROP_A, free_cancel_days=3650, cancellation_fee="First Night")
		self.rt = make_room_type(PROP_A, "WVR", base_price=1000)
		make_room(PROP_A, self.rt, "W101")
		make_room(PROP_A, self.rt, "W102")
		ensure_user(DESK, ["Front Desk"])
		ensure_user(MANAGER, ["Hotel Admin"])

	def book(self, n: int) -> str:
		return api.create_booking(
			property=PROP_A, room_type=self.rt, check_in_date=d(10),
			check_out_date=d(12), guest_name=f"UT Waiver Guest {n}",
			phone=f"+91 90000 7{n:04d}")["reservation"]

	def test_only_the_manager_waives_the_fee(self):
		desk_res, manager_res = self.book(1), self.book(2)

		frappe.set_user(DESK)  # nosemgrep: frappe-setuser -- test fixture
		self.assertFalse(api.cancellation_preview(desk_res)["can_waive"])
		with self.assertRaises(frappe.PermissionError):
			api.cancel_reservation(desk_res, waive_fee=1)
		self.assertEqual(frappe.db.get_value("Reservation", desk_res, "status"), "Confirmed")
		# the desk still cancels, and the policy fee lands
		out = api.cancel_reservation(desk_res)
		self.assertGreater(out["fee"], 0)
		self.assertFalse(out["waived"])

		frappe.set_user(MANAGER)  # nosemgrep: frappe-setuser -- test fixture
		self.assertTrue(api.cancellation_preview(manager_res)["can_waive"])
		out = api.cancel_reservation(manager_res, waive_fee=1)
		self.assertEqual(out["fee"], 0)
		self.assertTrue(out["waived"])
