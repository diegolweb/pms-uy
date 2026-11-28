"""OTA guest messages through Channex (HR-371): the thread index, the bell,
the 403 of a property without Channex's messages app, the desk's reply and
the webhook signal. No HTTP here: FakeMessages stands in for channex._request.
Names and texts are invented."""

import copy
import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from kamra import channel_messages as cmsg
from kamra.channels import channex
from kamra.tests.test_channex_feed import _ChannexConnectionMixin

SECRET = "Texto-del-huesped-que-no-se-guarda"


class FakeMessages:
	"""Channex's message_threads API in memory. Records every call as
	(method, path, params, payload)."""

	def __init__(self):
		self.threads: dict[str, dict] = {}
		self.calls: list[tuple] = []
		self.error: Exception | None = None

	def thread(self, tid, *, count=1, sender="guest", provider="BookingCom", booking=None,
	           prop="prop-ut") -> None:
		self.threads[tid] = {"type": "message_thread", "id": tid, "attributes": {
			"title": "Lucía Inventada", "is_closed": False, "provider": provider,
			"message_count": count, "inserted_at": "2026-11-20T10:00:00.000000",
			"last_message_received_at": "2026-11-26T12:00:00.000000",
			"last_message": {"message": SECRET, "attachments": [], "sender": sender,
			                 "inserted_at": "2026-11-26T12:00:00.000000"}},
			"relationships": {"property": {"data": {"type": "property", "id": prop}},
			                  "booking": {"data": {"type": "booking", "id": booking} if booking else None}}}

	def __call__(self, conn, method, path, *, params=None, payload=None):
		self.calls.append((method, path, params, copy.deepcopy(payload)))
		if self.error:
			raise self.error
		if method == "GET" and path == "message_threads":
			data = list(self.threads.values())
			page = int(params.get("pagination[page]") or 1)
			limit = int(params.get("pagination[limit]") or 10)
			return {"meta": {"page": page, "limit": limit, "total": len(data)},
			        "data": copy.deepcopy(data[(page - 1) * limit:page * limit])}
		if method == "POST" and path.endswith("/messages"):
			return {"data": {"type": "message", "id": "msg-new", "attributes": {
				"message": payload["message"]["message"], "sender": "property",
				"attachments": [], "inserted_at": "2026-11-26T13:00:00.000000"}}}
		raise AssertionError(f"FakeMessages: unexpected {method} {path}")


class ChannexMessagesTestCase(_ChannexConnectionMixin, IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.fake = FakeMessages()
		for key in (cmsg._OFF, cmsg._LIVE, cmsg._ERROR):
			frappe.cache.delete_value(key.format(connection=self.conn.name))
		# rows another test committed under a connection name that came back
		frappe.db.delete(cmsg.DT, {"connection": self.conn.name})
		self.tag = frappe.generate_hash(length=8)

	def th(self, n: str) -> str:
		"""A thread id of this test only."""
		return f"th-{n}-{self.tag}"

	def pull(self):
		with patch.object(frappe.db, "commit"), patch.object(channex, "_request", side_effect=self.fake):
			return cmsg.pull_messages(self.conn.name)

	def bells(self) -> list[dict]:
		return frappe.get_all("Kamra Notification", filters={
			"property": self.property, "reference_doctype": cmsg.DT},
			fields=["subject", "reference_name", "route"], order_by="creation asc")

	def booked(self, booking_id: str) -> str:
		guest = frappe.get_doc({"doctype": "Guest", "first_name": "Lucía",
			"last_name": "Inventada"}).insert(ignore_permissions=True).name
		res = frappe.get_doc({"doctype": "Reservation", "property": self.property,
			"room_type": self.room_type, "guest": guest, "status": "Confirmed",
			"check_in_date": add_days(nowdate(), 10), "check_out_date": add_days(nowdate(), 13),
			"adults": 2}).insert(ignore_permissions=True).name
		frappe.get_doc({"doctype": "Channel Booking Revision", "revision_id": "rev-" + booking_id,
			"connection": self.conn.name, "booking_id": booking_id, "status": "Acked",
			"results": json.dumps([{"result": "booked", "reservation": res}])}).insert(
			ignore_permissions=True)
		return res

	def test_sync_links_the_booking_and_rings_once_per_new_guest_message(self):
		res = self.booked(self.th("bk"))
		self.fake.thread(self.th("1"), count=2, booking=self.th("bk"))
		self.assertEqual(self.pull(), {"threads": 1, "rang": 0})  # first pass: quiet
		row = frappe.get_doc(cmsg.DT, self.th("1"))
		self.assertEqual((row.reservation, row.property, row.notified_count, row.last_sender),
		                 (res, self.property, 2, "guest"))
		self.assertEqual(self.bells(), [])

		self.fake.thread(self.th("1"), count=3, booking=self.th("bk"))  # the guest writes again
		self.assertEqual(self.pull()["rang"], 1)
		self.assertEqual(self.pull()["rang"], 0)  # nothing new: no second ring
		guest = frappe.db.get_value("Reservation", res, "guest_name")
		self.assertEqual([(b.subject, b.reference_name, b.route) for b in self.bells()],
		                 [(f"New message from Booking.com: {guest}", self.th("1"), f"/ota-messages?thread={self.th('1')}")])

		self.fake.thread(self.th("1"), count=4, sender="property", booking=self.th("bk"))  # the hotel answered
		self.fake.thread(self.th("2"), count=1, provider="Expedia")  # a new thread, no booking yet
		self.assertEqual(self.pull()["rang"], 1)
		self.assertEqual([b.subject for b in self.bells()][1:],
		                 ["New message from Expedia: Guest from Expedia"])
		self.assertEqual(frappe.db.get_value(cmsg.DT, self.th("1"), "notified_count"), 4)

		# a thread that cannot be indexed is logged with ids only
		self.fake.thread(self.th("3"), count=1)
		with patch.object(cmsg, "_reservation_for", side_effect=ValueError(SECRET)):
			self.fake.thread(self.th("3"), count=1, booking="bk-x")
			self.pull()
		logs = frappe.get_all("Error Log", filters={"method": f"Channex messages {self.conn.name}"},
		                      fields=["error"], order_by="creation desc", limit=1)
		self.assertTrue(logs)
		self.assertIn(self.th("3"), logs[0].error)
		for row in frappe.get_all(cmsg.DT, filters={"property": self.property}, fields=["*"]):
			self.assertNotIn(SECRET, json.dumps(row, default=str))
		self.assertNotIn(SECRET, logs[0].error)

	def test_without_the_messages_app_the_pull_pauses_and_the_desk_sees_app_missing(self):
		self.fake.thread(self.th("1"))
		self.fake.error = channex.ChannexError(403, "forbidden")
		self.assertEqual(self.pull(), {"skipped": "messaging_off"})
		calls = len(self.fake.calls)
		self.assertEqual(self.pull(), {"skipped": "messaging_off"})
		self.assertEqual(len(self.fake.calls), calls)  # paused: Channex is not asked again
		self.assertFalse(frappe.db.exists(cmsg.DT, {"connection": self.conn.name}))
		frappe.cache.delete_value(cmsg._OFF.format(connection=self.conn.name))
		with patch.object(channex, "_request", side_effect=self.fake):
			out = cmsg.threads(property=self.property)
		self.assertTrue(out["app_missing"])
		self.assertEqual(out["threads"], [])

	def test_reply_posts_to_its_thread_and_refuses_empty_text_or_another_hotels_thread(self):
		self.fake.thread(self.th("1"), count=2)
		self.pull()
		with patch.object(channex, "_request", side_effect=self.fake):
			out = cmsg.reply(property=self.property, thread=self.th("1"), text="  Sí, el desayuno está incluido. ")
		posts = [c for c in self.fake.calls if c[0] == "POST"]
		self.assertEqual(posts, [("POST", f"message_threads/{self.th('1')}/messages", None,
		                          {"message": {"message": "Sí, el desayuno está incluido."}})])
		self.assertEqual((out["sender"], out["text"]), ("property", "Sí, el desayuno está incluido."))
		row = frappe.get_doc(cmsg.DT, self.th("1"))
		self.assertEqual((row.last_sender, row.message_count, row.notified_count), ("property", 3, 3))

		other = frappe.get_doc({"doctype": "Property", "property_name": "TEST CHX OTRO " + frappe.generate_hash(length=6),
			"country": "Uruguay", "currency": "USD"}).insert(ignore_permissions=True).name
		other_conn = frappe.get_doc({"doctype": "Channel Manager Connection", "property": other,
			"provider": "Channex", "active": 1, "external_property_id": "prop-otro",
			"webhook_secret": "ut-secret"}).insert(ignore_permissions=True).name
		frappe.get_doc({"doctype": cmsg.DT, "thread_id": self.th("otro"), "connection": other_conn,
			"property": other, "provider": "BookingCom", "message_count": 1,
			"last_sender": "guest"}).insert(ignore_permissions=True)
		with patch.object(channex, "_request", side_effect=self.fake):
			with self.assertRaises(frappe.ValidationError):
				cmsg.reply(property=self.property, thread=self.th("1"), text="   ")
			with self.assertRaises(frappe.DoesNotExistError):
				cmsg.reply(property=self.property, thread=self.th("otro"), text="Hola")
		self.assertEqual(len([c for c in self.fake.calls if c[0] == "POST"]), 1)

	# ── HR-382: an Airbnb thread enters the index only as a hotel stay ──

	def test_an_airbnb_thread_not_positively_a_hotel_stay_leaves_no_trace(self):  # (m1)
		self.fake.thread(self.th("bdc"), count=1)  # a Booking thread: the next pass is not the quiet one
		self.pull()
		# a Channex booking whose revision never applied (a host's own listing)
		frappe.get_doc({"doctype": "Channel Booking Revision", "revision_id": "rev-" + self.th("bk-casa"),
			"connection": self.conn.name, "booking_id": self.th("bk-casa"), "status": "Failed"}).insert(
			ignore_permissions=True)
		self.fake.thread(self.th("casa"), count=2, provider="AirBNB", booking=self.th("bk-casa"))
		self.fake.thread(self.th("consulta"), count=1, provider="AirBNB")  # an inquiry: no booking
		calls = len(self.fake.calls)
		self.assertEqual(self.pull()["rang"], 0)
		for n in ("casa", "consulta"):
			self.assertFalse(frappe.db.exists(cmsg.DT, self.th(n)), n)
		self.assertEqual(self.bells(), [])
		self.assertEqual({(c[0], c[1]) for c in self.fake.calls[calls:]}, {("GET", "message_threads")})
		# the desk lists and caches only the hotel's thread
		with patch.object(channex, "_request", side_effect=self.fake):
			out = cmsg.threads(property=self.property)
		self.assertEqual([t["thread"] for t in out["threads"]], [self.th("bdc")])
		cached = frappe.cache.get_value(cmsg._LIVE.format(connection=self.conn.name))
		self.assertEqual(set(cached["previews"]), {self.th("bdc")})
		self.assertEqual({(c[0], c[1]) for c in self.fake.calls[calls:]}, {("GET", "message_threads")})

	def test_an_airbnb_thread_of_an_applied_booking_is_indexed_and_rings_once(self):  # (m2)
		self.fake.thread(self.th("bdc"), count=1)
		self.pull()
		res = self.booked(self.th("bk"))
		self.fake.thread(self.th("abb"), count=1, provider="AirBNB", booking=self.th("bk"))
		self.assertEqual(self.pull()["rang"], 1)
		self.assertEqual(self.pull()["rang"], 0)
		row = frappe.get_doc(cmsg.DT, self.th("abb"))
		self.assertEqual((row.reservation, row.provider, row.property), (res, "AirBNB", self.property))
		guest = frappe.db.get_value("Reservation", res, "guest_name")
		self.assertEqual([b.subject for b in self.bells()], [f"New message from Airbnb: {guest}"])

	def test_message_webhook_without_data_queues_the_pull(self):
		from kamra.tests.test_channex_feed import ChannexWebhookTestCase
		for event in ("message", "message_thread_booking_assigned"):
			body = {"event": event, "property_id": "prop-ut", "user_id": None,
			        "timestamp": "2026-11-26T12:00:00Z"}
			with patch("frappe.enqueue") as enqueue:
				out = ChannexWebhookTestCase.post(self, body, {"X-Webhook-Secret": "ut-secret"})
			self.assertEqual(out, {"ok": True, "queued": True}, event)
			self.assertEqual(enqueue.call_count, 1, event)
			self.assertEqual(enqueue.call_args.args[0], "kamra.channel_messages.pull_messages")
			self.assertEqual(enqueue.call_args.kwargs["job_id"], f"ota-msgs::{self.conn.name}")
			self.assertTrue(enqueue.call_args.kwargs["deduplicate"])
		cron = frappe.get_hooks("scheduler_events")["cron"]["* * * * *"]
		self.assertIn("kamra.channel_messages.enqueue_message_pulls", cron)
