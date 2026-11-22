"""Group blocks out to Channex through the ARI outbox, as they are saved.

A Confirmed Group Booking holds its unpicked rooms off sale
(kamra.group_blocks), and channel_manager.ari_snapshot already takes them
off the availability it reports. These tests pin that the group's own save
queues its nights for availability, so the platforms learn of a block when
it is made, not with the nightly sweep: certification case 10 as a group
block, then moving its dates, moving it to another property, cancelling
it, deleting it, an edit that moves nothing and an enqueue that fails.

No HTTP: FakeChannex stands in for channex._send. Dates are relative to
today."""

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from kamra.tests.test_channex_outbox import _AriFixture, _AriWorkerMixin, day


class ChannexGroupBlocksTestCase(_AriWorkerMixin, _AriFixture, IntegrationTestCase):
	def group(self, start: int = 10, nights: int = 7, rooms: int = 5, status: str = "Confirmed"):
		return frappe.get_doc({"doctype": "Group Booking", "property": self.property,
			"group_name": "UT Cert 10", "check_in_date": day(start),
			"check_out_date": day(start + nights), "status": status,
			"blocks": [{"room_type": self.twin, "rooms_blocked": rooms}]}).insert(ignore_permissions=True)

	def pick(self, gb, start: int, nights: int) -> str:
		"""A Twin picked up from the group's block, on its own nights."""
		guest = frappe.get_doc({"doctype": "Guest", "first_name": "Ari",
			"last_name": "Grupo"}).insert(ignore_permissions=True).name
		return frappe.get_doc({"doctype": "Reservation", "property": self.property,
			"room_type": self.twin, "guest": guest, "status": "Confirmed", "booking_type": "Group",
			"group_booking": gb.name, "check_in_date": day(start), "check_out_date": day(start + nights),
			"adults": 2}).insert(ignore_permissions=True).name

	def group_rows(self) -> list[tuple]:
		return [(r.kinds, str(r.date_from), str(r.date_to)) for r in self.rows(status="Pending")
		        if (r.reason or "").startswith("Group Booking")]

	@contextmanager
	def on(self, conn):
		"""The fixture's helpers (flush, rows, baseline, clear_cache) on
		another connection."""
		mine, self.conn = self.conn, conn
		try:
			yield
		finally:
			self.conn = mine

	def second_property(self) -> frappe._dict:
		"""Property B: its own Twin (8 rooms), mapped to rt-twin-b on a Channex
		connection of its own (cx-test-b), and what it has already 'sent'."""
		tag = frappe.generate_hash(length=6).upper()
		b = frappe._dict(property=frappe.get_doc({"doctype": "Property",
			"property_name": "TEST ARI B " + tag, "country": "Uruguay",
			"currency": "USD"}).insert(ignore_permissions=True).name)
		b.twin = frappe.get_doc({"doctype": "Room Type", "property": b.property,
			"room_type_code": "TWB" + tag, "room_type_name": "Twin", "base_price": 100,
			"adults_capacity": 2}).insert(ignore_permissions=True).name
		for i in range(8):
			frappe.get_doc({"doctype": "Room", "property": b.property, "room_type": b.twin,
				"room_number": f"TWB{tag}-{i + 1}"}).insert(ignore_permissions=True)
		b.conn = frappe.get_doc({"doctype": "Channel Manager Connection",
			"property": b.property, "provider": "Channex", "active": 1,
			"external_property_id": "cx-test-b", "webhook_secret": "ut-hook-b",
			"ari_batch_seconds": 0}).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "Channel Room Mapping", "connection": b.conn.name,
			"room_type": b.twin, "external_room_id": "rt-twin-b",
			"external_rate_id": "rp-twin-b-bar"}).insert(ignore_permissions=True)
		with self.on(b.conn):
			self.clear_cache()
			self.baseline(self.BASELINE_DAYS)

		def forget():
			with self.on(b.conn):
				self.clear_cache()
		self.addCleanup(forget)
		return b

	def availability(self, property_id: str | None = None) -> list[list[dict]]:
		"""The availability POSTs, in order; with property_id, only that
		Channex property's."""
		return [call for call in self.fake.posts("availability")
		        if not property_id or (call and call[0]["property_id"] == property_id)]

	@staticmethod
	def ranges(call: list[dict]) -> list[tuple]:
		return sorted((v["room_type_id"], v.get("date_from") or v["date"], v.get("date_to") or v["date"],
		               v["availability"]) for v in call)

	# ------------------------------------------------------- certification 10
	def test_cert_10_group_block_ranges(self):
		"""Case 10 from the groups screen: a Confirmed group of 5 Twin for 7
		nights queues one availability row with its nights, and the flush
		sends 1 POST availability with Twin 3 (8 - 5) on those nights."""
		self.group(10, 7, 5)
		rows = self.rows()
		self.assertEqual([(r.status, r.kinds, str(r.date_from), str(r.date_to)) for r in rows],
		                 [("Pending", "availability", day(10), day(16))])
		self.assertIn("Group Booking", rows[0].reason)
		self.flush()
		self.assertEqual(self.fake.posts("restrictions"), [])
		self.assertEqual(self.availability(), [[
			{"property_id": "cx-test", "room_type_id": "rt-twin", "date_from": day(10), "date_to": day(16),
			 "availability": 3}]])

	def test_moving_the_dates_covers_the_old_nights_and_the_new(self):
		gb = self.group(10, 7, 5)
		self.flush()
		gb.reload()
		gb.check_in_date, gb.check_out_date = day(20), day(27)
		gb.save(ignore_permissions=True)
		pending = self.rows(status="Pending")
		self.assertEqual([(str(r.date_from), str(r.date_to)) for r in pending], [(day(10), day(26))])
		self.flush()
		self.assertEqual(len(self.availability()), 2)
		self.assertEqual(self.ranges(self.availability()[1]), [
			("rt-twin", day(10), day(16), 8), ("rt-twin", day(20), day(26), 3)])

	def test_moving_the_group_to_another_property_tells_both(self):
		"""HR-198: a Confirmed group of 5 Twin moved from property A to B, on
		the same nights (its block now on B's Twin), queues availability with
		the group's nights in A and in B. The flushes send Twin 8 in A, where
		its rooms go back on sale, and Twin 3 in B."""
		b = self.second_property()
		gb = self.group(10, 7, 5)
		self.flush()
		self.assertEqual(self.ranges(self.availability("cx-test")[0]), [("rt-twin", day(10), day(16), 3)])
		gb.reload()
		gb.property = b.property
		gb.blocks[0].room_type = b.twin
		gb.save(ignore_permissions=True)
		self.assertEqual(self.group_rows(), [("availability", day(10), day(16))])
		with self.on(b.conn):
			self.assertEqual(self.group_rows(), [("availability", day(10), day(16))])
		self.flush()
		with self.on(b.conn):
			self.flush()
		self.assertEqual(len(self.availability("cx-test")), 2)
		self.assertEqual(self.ranges(self.availability("cx-test")[1]), [("rt-twin", day(10), day(16), 8)])
		self.assertEqual(self.availability("cx-test-b"), [[
			{"property_id": "cx-test-b", "room_type_id": "rt-twin-b", "date_from": day(10), "date_to": day(16),
			 "availability": 3}]])

	def test_changing_only_the_property_tells_the_one_it_left(self):
		"""The Desk's edit: only the group's Property changes, its block still
		names A's Twin. A no longer holds it (Twin 8 there) and B is queued
		too, but none of B's rooms are held, so B's flush sends nothing."""
		b = self.second_property()
		gb = self.group(10, 7, 5)
		self.flush()
		gb.reload()
		gb.property = b.property
		gb.save(ignore_permissions=True)
		self.assertEqual(self.group_rows(), [("availability", day(10), day(16))])
		with self.on(b.conn):
			self.assertEqual(self.group_rows(), [("availability", day(10), day(16))])
		self.flush()
		with self.on(b.conn):
			self.flush()
		self.assertEqual(self.ranges(self.availability("cx-test")[-1]), [("rt-twin", day(10), day(16), 8)])
		self.assertEqual(self.availability("cx-test-b"), [])

	def test_cancelling_the_group_gives_the_rooms_back_in_one_call(self):
		gb = self.group(10, 7, 5)
		self.flush()
		gb.reload()
		gb.status = "Cancelled"
		gb.save(ignore_permissions=True)
		self.flush()
		self.assertEqual(len(self.availability()), 2)
		self.assertEqual(self.ranges(self.availability()[1]), [("rt-twin", day(10), day(16), 8)])

	def test_cancelling_from_its_rooms_gives_back_every_night_of_the_block(self):
		"""HR-189: group_cancel (the groups screen's cancel) with 1 of the 5
		Twin picked up, on 2 of the 7 nights, cancels the room and then the
		group. The group's nights go out whole, in the same call as the
		room's: Twin 8 on all 7, not only on the 2 the room covered."""
		from kamra.api import group_cancel
		gb = self.group(10, 7, 5)
		picked = self.pick(gb, 12, 2)
		self.flush()
		self.assertEqual(self.ranges(self.availability()[0]), [("rt-twin", day(10), day(16), 3)])
		group_cancel(gb.name, waive_fee=1)
		self.assertEqual(frappe.db.get_value("Reservation", picked, "status"), "Cancelled")
		self.assertEqual(frappe.db.get_value("Group Booking", gb.name, "status"), "Cancelled")
		self.assertEqual(self.group_rows(), [("availability", day(10), day(16))])
		self.flush()
		self.assertEqual(len(self.availability()), 2)
		self.assertEqual(self.ranges(self.availability()[1]), [("rt-twin", day(10), day(16), 8)])

	def test_cancelling_some_of_its_rooms_keeps_the_block(self):
		"""A room cancelled while another is still booked goes back to the
		block, not on sale: the group stays Confirmed, nothing of it is
		queued and Twin stays 3."""
		from kamra.api import group_cancel
		gb = self.group(10, 7, 5)
		first, second = self.pick(gb, 12, 2), self.pick(gb, 10, 7)
		self.flush()
		group_cancel(gb.name, [first], waive_fee=1)
		self.assertEqual(frappe.db.get_value("Reservation", second, "status"), "Confirmed")
		self.assertEqual(frappe.db.get_value("Group Booking", gb.name, "status"), "Confirmed")
		self.assertEqual(self.group_rows(), [])
		self.flush()
		self.assertEqual([self.ranges(c) for c in self.availability()], [[("rt-twin", day(10), day(16), 3)]])

	def test_deleting_the_group_gives_the_rooms_back_in_one_call(self):
		gb = self.group(10, 7, 5)
		self.flush()
		frappe.delete_doc("Group Booking", gb.name, ignore_permissions=True, force=True)
		self.flush()
		self.assertEqual(len(self.availability()), 2)
		self.assertEqual(self.ranges(self.availability()[1]), [("rt-twin", day(10), day(16), 8)])

	def test_changing_the_rooms_of_the_block_sends_the_new_number(self):
		gb = self.group(10, 7, 5)
		self.flush()
		gb.reload()
		gb.blocks[0].rooms_blocked = 6
		gb.save(ignore_permissions=True)
		self.flush()
		self.assertEqual(self.ranges(self.availability()[1]), [("rt-twin", day(10), day(16), 2)])

	def test_confirming_an_open_group_sends_its_block(self):
		gb = self.group(10, 7, 5, status="Open")
		self.assertEqual(self.rows(), [])  # a proposal holds nothing
		gb.reload()
		gb.status = "Confirmed"
		gb.save(ignore_permissions=True)
		self.flush()
		self.assertEqual(self.availability(), [[
			{"property_id": "cx-test", "room_type_id": "rt-twin", "date_from": day(10), "date_to": day(16),
			 "availability": 3}]])

	def test_changing_only_the_notes_queues_nothing(self):
		gb = self.group(10, 7, 5)
		self.flush()
		gb.reload()
		gb.notes = "Late arrival, rooms on one floor"
		gb.save(ignore_permissions=True)
		self.assertEqual(self.rows(status="Pending"), [])
		self.flush()
		self.assertEqual(len(self.availability()), 1)

	def test_a_failed_enqueue_does_not_stop_the_group_saving(self):
		# log_error is patched, not counted: an Error Log outlives the test's
		# rollback and test_channex_outbox counts them from zero
		with patch("kamra.channel_ari._insert_row", side_effect=RuntimeError("outbox down")), \
				patch("frappe.log_error") as log_error:
			gb = self.group(10, 7, 5)
		self.assertTrue(frappe.db.exists("Group Booking", gb.name))
		self.assertEqual(self.rows(), [])
		self.assertIn("Channex ARI enqueue", [c.kwargs.get("title") for c in log_error.call_args_list])
