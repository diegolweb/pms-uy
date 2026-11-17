"""Channex inbound: booking revisions feed, ack after commit, idempotency.

Channex's booking webhook is a signal (ids only); the booking itself is read
from the booking_revisions feed, applied, committed and only then acked -
Channex wipes the guest's contact data from an acked revision. No HTTP here:
FakeChannex stands in for channex._request."""

import copy
import hashlib
import hmac
import json
import time
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import add_days, nowdate

from kamra import channel_manager as cm
from kamra.channels import channex


def doc_revision(**kw) -> dict:
	"""The booking revision of Channex's public documentation: one room on
	Booking.com, in GBP, with breakfast and a card guarantee."""
	rev = {
		"id": "6f3f2b9c-rev-doc", "property_id": "prop-doc", "booking_id": "b-doc",
		"unique_id": "BDC-9996013801", "ota_reservation_code": "9996013801",
		"ota_name": "Booking.com", "status": "new",
		"arrival_date": "2019-04-26", "departure_date": "2019-04-27", "arrival_hour": "10:00",
		"amount": "220.00", "currency": "GBP", "ota_commission": "10.00",
		"notes": "Non-smoking room", "inserted_at": "2019-04-23T12:22:21.000000",
		"customer": {"name": "John", "surname": "Smith", "phone": "+44 7700 900123",
		             "mail": "john.smith@example.test", "address": "1 Test Street", "country": "GB"},
		"services": [{"name": "Breakfast", "type": "Breakfast", "persons": 2, "nights": 1,
		              "total_price": "20.00"}],
		"guarantee": {"card_number": "4111111111111111", "card_type": "visa",
		              "cardholder_name": "John Smith", "cvv": "123", "expiration_date": "10/2030"},
		"rooms": [{"checkin_date": "2019-04-26", "checkout_date": "2019-04-27",
		           "room_type_id": "rt-doc", "rate_plan_id": "rp-doc", "amount": "200.00",
		           "days": {"2019-04-26": "200.00"},
		           "occupancy": {"adults": 2, "children": 0, "infants": 0}}],
	}
	rev.update(kw)
	return rev


class ChannexRevisionParsingTestCase(UnitTestCase):
	def test_documented_revision_is_one_book_event(self):
		events = channex.revision_events(doc_revision())
		self.assertEqual(len(events), 1)
		e = events[0]
		self.assertEqual(e["event"], "book")
		self.assertEqual(e["ota_ref"], "9996013801")
		self.assertEqual(e["booking_id"], "9996013801")
		self.assertEqual(e["revision_id"], "6f3f2b9c-rev-doc")
		self.assertEqual(e["channel"], "Booking.com")
		self.assertEqual(e["room_type_external_id"], "rt-doc")
		self.assertEqual(e["external_rate_id"], "rp-doc")
		self.assertEqual((e["check_in"], e["check_out"]), ("2019-04-26", "2019-04-27"))
		self.assertEqual(e["total"], 220.0)
		self.assertEqual(e["nightly"], [{"date": "2019-04-26", "amount": 200.0}])
		self.assertEqual(e["commission"], 10.0)
		self.assertEqual(e["adults"], 2)
		self.assertEqual(e["currency"], "GBP")
		self.assertEqual(e["guest_name"], "John Smith")
		self.assertEqual(e["email"], "john.smith@example.test")
		self.assertIn("Non-smoking room", e["notes"])
		self.assertIn("Breakfast", e["notes"])
		self.assertIn("10:00", e["notes"])

	def test_two_rooms_split_ref_total_and_commission(self):
		rooms = [dict(doc_revision()["rooms"][0], room_type_id="rt-a", amount="200.00",
		              days={"2019-04-26": "200.00"}),
		         dict(doc_revision()["rooms"][0], room_type_id="rt-b", amount="100.00",
		              days={"2019-04-26": "100.00"})]
		events = channex.revision_events(doc_revision(
			ota_reservation_code="X", amount="300.00", ota_commission="30.00", rooms=rooms))
		self.assertEqual([e["ota_ref"] for e in events], ["X-0", "X-1"])
		self.assertEqual([e["booking_id"] for e in events], ["X", "X"])
		self.assertEqual([e["total"] for e in events], [200.0, 100.0])
		self.assertEqual([e["commission"] for e in events], [20.0, 10.0])
		self.assertEqual([e["room_type_external_id"] for e in events], ["rt-a", "rt-b"])

	def test_modified_and_cancelled_statuses(self):
		self.assertEqual({e["event"] for e in channex.revision_events(doc_revision(status="modified"))},
		                 {"modify"})
		events = channex.revision_events(doc_revision(status="cancelled"))
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["event"], "cancel")
		self.assertEqual(events[0]["booking_id"], "9996013801")
		self.assertEqual(events[0]["ota_ref"], "9996013801")
		self.assertEqual(events[0]["channel"], "Booking.com")

	def test_card_guarantee_never_reaches_the_events(self):
		for status in ("new", "modified", "cancelled"):
			dumped = json.dumps(channex.revision_events(doc_revision(status=status)))
			self.assertNotIn("411111", dumped)
			self.assertNotIn("guarantee", dumped)
			self.assertNotIn("cvv", dumped)

	def test_signal_webhook_parses_to_nothing(self):
		conn = frappe._dict(name="CMC-X", provider="Channex", external_property_id="prop-doc")
		self.assertEqual(channex.parse_webhook(
			conn, {"event": "booking", "payload": {"booking_id": "b", "revision_id": "r"}}), [])

	def test_full_payload_webhook_still_parses_as_a_revision(self):
		conn = frappe._dict(name="CMC-X", provider="Channex", external_property_id="prop-doc")
		events = channex.parse_webhook(conn, {"event": "booking", "payload": doc_revision()})
		self.assertEqual([e["ota_ref"] for e in events], ["9996013801"])

	def test_verify_signature(self):
		secret, body, ts = "s3cret", b'{"event":"booking"}', "1763251200"
		good = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
		now = float(ts) + 10
		self.assertTrue(channex.verify_signature(secret, ts, good, body, now=now))
		self.assertFalse(channex.verify_signature(secret, ts, "0" * 64, body, now=now))
		self.assertFalse(channex.verify_signature(secret, ts, good, body + b" ", now=now))
		self.assertFalse(channex.verify_signature(secret, ts, good, body, now=float(ts) + 301))
		self.assertTrue(channex.verify_signature(secret, ts, f"{'0' * 64},{good}", body, now=now))
		self.assertFalse(channex.verify_signature(secret, None, good, body, now=now))
		self.assertFalse(channex.verify_signature(secret, ts, None, body, now=now))
		self.assertFalse(channex.verify_signature(secret, "not-a-number", good, body, now=now))


class _Response:
	def __init__(self, status_code, body=None, headers=None):
		self.status_code, self._body, self.headers = status_code, body, headers or {}
		self.text = json.dumps(body) if body is not None else ""

	def json(self):
		if self._body is None:
			raise ValueError("no json")
		return self._body


class ChannexRequestTestCase(UnitTestCase):
	def conn(self):
		c = frappe._dict(name="CMC-X", provider="Channex", endpoint="http://channex.test/api/v1",
		                 external_property_id="prop-1")
		c.get_password = lambda *a, **k: "key-1"
		return c

	def test_fetch_feed_asks_for_the_oldest_unacked_first(self):
		body = {"meta": {"page": 1}, "data": [
			{"type": "booking_revision", "id": "r1", "attributes": {"id": "r1", "status": "new"}},
			{"type": "booking_revision", "id": "r2", "attributes": {"status": "new"}}]}
		with patch("requests.request", return_value=_Response(200, body)) as req:
			revs = channex.fetch_feed(self.conn())
		self.assertEqual([r["id"] for r in revs], ["r1", "r2"])
		args, kwargs = req.call_args
		self.assertEqual(args[0], "GET")
		self.assertEqual(args[1], "http://channex.test/api/v1/booking_revisions/feed")
		self.assertEqual(kwargs["params"], {"filter[property_id]": "prop-1",
			"order[inserted_at]": "asc", "pagination[page]": 1, "pagination[limit]": 100})
		self.assertEqual(kwargs["headers"]["user-api-key"], "key-1")
		self.assertEqual(kwargs["timeout"], 20)

	def test_429_raises_rate_limited_with_retry_after(self):
		with patch("requests.request", return_value=_Response(429, {"errors": {"code": "too_many"}},
		                                                     {"Retry-After": "30"})):
			with self.assertRaises(channex.ChannexRateLimited) as ctx:
				channex.fetch_feed(self.conn())
		self.assertEqual(ctx.exception.retry_after, 30)
		self.assertEqual(ctx.exception.status, 429)

	def test_429_without_a_usable_retry_after_waits_60(self):
		with patch("requests.request", return_value=_Response(429, None, {"Retry-After": "soon"})):
			with self.assertRaises(channex.ChannexRateLimited) as ctx:
				channex.fetch_feed(self.conn())
		self.assertEqual(ctx.exception.retry_after, 60)

	def test_other_error_is_a_short_channex_error(self):
		with patch("requests.request", return_value=_Response(401, {"errors": {"code": "unauthorized",
		                                                                      "title": "x" * 900}})):
			with self.assertRaises(channex.ChannexError) as ctx:
				channex.fetch_feed(self.conn())
		self.assertNotIsInstance(ctx.exception, channex.ChannexRateLimited)
		self.assertEqual(ctx.exception.status, 401)
		self.assertLessEqual(len(ctx.exception.detail), 300)

	def test_ack_posts_to_the_revision(self):
		with patch("requests.request", return_value=_Response(200, {"meta": {"message": "Success"}})) as req:
			channex.ack_revision(self.conn(), "r1")
		self.assertEqual(req.call_args[0][:2], ("POST", "http://channex.test/api/v1/booking_revisions/r1/ack"))

	def test_fetch_revision_returns_its_attributes(self):
		body = {"data": {"type": "booking_revision", "id": "r1", "attributes": {"status": "new"}}}
		with patch("requests.request", return_value=_Response(200, body)) as req:
			rev = channex.fetch_revision(self.conn(), "r1")
		self.assertEqual(rev, {"id": "r1", "status": "new"})
		self.assertEqual(req.call_args[0][1], "http://channex.test/api/v1/booking_revisions/r1")


class FakeChannex:
	"""Channex's booking_revisions API in memory, standing in for
	channex._request. Records every call as (method, path, params) and, in the
	shared order list, every ack next to the test's commits."""

	def __init__(self, order: list):
		self.revisions: dict[str, dict] = {}
		self.acked: set[str] = set()
		self.calls: list[tuple] = []
		self.order = order
		self.failures: dict[tuple, list] = {}

	def serve(self, rev: dict) -> None:
		"""Put a revision in the feed (again, unacked, if it was acked)."""
		self.revisions[rev["id"]] = copy.deepcopy(rev)
		self.acked.discard(rev["id"])

	def fail(self, method: str, path: str, exc: Exception, times: int = 1) -> None:
		self.failures.setdefault((method, path), []).extend([exc] * times)

	def acks(self) -> list[str]:
		return [p.split("/")[1] for m, p, _ in self.calls if m == "POST" and p.endswith("/ack")]

	def __call__(self, conn, method, path, *, params=None, payload=None):
		self.calls.append((method, path, params))
		if method == "POST" and path.endswith("/ack"):
			self.order.append(("ack", path.split("/")[1]))
		pending = self.failures.get((method, path))
		if pending:
			raise pending.pop(0)
		if method == "GET" and path == "booking_revisions/feed":
			data = [{"type": "booking_revision", "id": rid, "attributes": copy.deepcopy(r)}
			        for rid, r in self.revisions.items() if rid not in self.acked]
			page = int((params or {}).get("pagination[page]") or 1)
			limit = int((params or {}).get("pagination[limit]") or 10)
			return {"meta": {"page": page, "limit": limit, "total": len(data)},
			        "data": data[(page - 1) * limit:page * limit]}
		if method == "POST" and path.endswith("/ack"):
			self.acked.add(path.split("/")[1])
			return {"meta": {"message": "Success"}}
		if method == "GET" and path.startswith("booking_revisions/"):
			rid = path.split("/")[1]
			return {"data": {"type": "booking_revision", "id": rid,
			                 "attributes": copy.deepcopy(self.revisions[rid])}}
		raise AssertionError(f"FakeChannex: unexpected {method} {path}")


class _ChannexConnectionMixin:
	def setUp(self):
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		frappe.local.lang = "en"
		self.property = frappe.get_doc({"doctype": "Property",
			"property_name": "TEST CHX " + frappe.generate_hash(length=8),
			"country": "Uruguay", "currency": "USD"}).insert(ignore_permissions=True).name
		self.room_type = frappe.get_doc({"doctype": "Room Type", "property": self.property,
			"room_type_code": "STD", "room_type_name": "Standard", "base_price": 100,
			"adults_capacity": 2}).insert(ignore_permissions=True).name
		for number in ("101", "102"):
			frappe.get_doc({"doctype": "Room", "property": self.property,
				"room_type": self.room_type, "room_number": number}).insert(ignore_permissions=True)
		self.conn = frappe.get_doc({"doctype": "Channel Manager Connection", "property": self.property,
			"provider": "Channex", "active": 1, "external_property_id": "prop-ut",
			"webhook_secret": "ut-secret"}).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "Channel Room Mapping", "connection": self.conn.name,
			"room_type": self.room_type, "external_room_id": "rt-std"}).insert(ignore_permissions=True)
		# connection names come back after each test's rollback; their cache keys must not
		frappe.cache.delete_value(f"channex_pause::{self.conn.name}")
		frappe.cache.delete_value(f"channex_feed_error::{self.conn.name}")


class ChannexFeedTestCase(_ChannexConnectionMixin, IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.order: list = []
		self.fake = FakeChannex(self.order)

	def rev(self, code=None, status="new", nights=1, rooms=None, phone="+598 99 765 432", **kw) -> dict:
		tag = frappe.generate_hash(length=8)
		code = code or "UT-" + tag
		arrival, departure = nowdate(), str(add_days(nowdate(), nights))
		days = {str(add_days(nowdate(), i)): "100.00" for i in range(nights)}
		rev = {"id": "rev-" + frappe.generate_hash(length=12), "property_id": "prop-ut",
			"booking_id": "book-" + code, "unique_id": "BDC-" + code, "ota_reservation_code": code,
			"ota_name": "Booking.com", "status": status, "arrival_date": arrival,
			"departure_date": departure, "arrival_hour": "15:00",
			"amount": f"{100 * nights:.2f}", "currency": "USD", "ota_commission": "15.00",
			"notes": "Quiet room please", "inserted_at": "2026-11-16T03:00:00.000000",
			"customer": {"name": "Ana", "surname": "Prueba", "phone": phone,
			             "mail": f"ana-{tag}@example.test"},
			"services": [], "guarantee": {"card_number": "4111111111111111", "cvv": "123"},
			"rooms": rooms or [{"checkin_date": arrival, "checkout_date": departure,
				"room_type_id": "rt-std", "rate_plan_id": "rp-std", "amount": f"{100 * nights:.2f}",
				"days": days, "occupancy": {"adults": 2, "children": 0, "infants": 0}}]}
		rev.update(kw)
		return rev

	def pull(self):
		"""One pull_feed pass as the worker runs it, against FakeChannex. Each
		commit is a savepoint (and an entry in self.order with the reservations
		that exist at that moment); a bare rollback goes back to the last one."""
		state = {"sp": "chx_" + frappe.generate_hash(length=8)}
		frappe.db.savepoint(state["sp"])
		rollback = frappe.db.rollback

		def worker_commit(*args, **kwargs):
			self.order.append(("commit", frappe.db.count("Reservation", {"property": self.property})))
			state["sp"] = "chx_" + frappe.generate_hash(length=8)
			frappe.db.savepoint(state["sp"])

		def worker_rollback(*args, **kwargs):
			if args or kwargs:
				return rollback(*args, **kwargs)
			self.order.append(("rollback",))
			return rollback(save_point=state["sp"])

		with patch.object(frappe.db, "commit", side_effect=worker_commit), \
		     patch.object(frappe.db, "rollback", side_effect=worker_rollback), \
		     patch.object(channex, "_request", side_effect=self.fake):
			return cm.pull_feed(self.conn.name)

	def reservations(self, code) -> list[dict]:
		return frappe.get_all("Reservation", filters={"property": self.property,
			"ota_ref": ("like", code + "%")}, fields=["name", "status", "ota_ref", "check_out_date",
			"guest", "special_requests"], order_by="ota_ref asc")

	def row(self, rev) -> "frappe.model.document.Document":
		return frappe.get_doc("Channel Booking Revision", rev["id"])

	def sold_out(self):
		guest = frappe.get_doc({"doctype": "Guest", "first_name": "Walk In"}).insert(ignore_permissions=True).name
		for _ in range(2):
			frappe.get_doc({"doctype": "Reservation", "property": self.property,
				"room_type": self.room_type, "guest": guest, "status": "Confirmed",
				"check_in_date": nowdate(), "check_out_date": add_days(nowdate(), 1),
				"adults": 2}).insert(ignore_permissions=True)

	def test_revision_row_rejects_a_duplicate_revision_id(self):
		row = {"doctype": "Channel Booking Revision", "connection": self.conn.name,
			"revision_id": "rev-dup-" + frappe.generate_hash(length=8), "booking_id": "b"}
		frappe.get_doc(dict(row)).insert(ignore_permissions=True)
		with self.assertRaises(frappe.DuplicateEntryError):
			frappe.get_doc(dict(row)).insert(ignore_permissions=True)

	def test_new_revision_books_commits_then_acks_once(self):
		rev = self.rev()
		self.fake.serve(rev)
		out = self.pull()
		self.assertEqual(out, {"acked": 1, "failed": 0, "skipped": 0})
		res = self.reservations(rev["ota_reservation_code"])
		self.assertEqual([(r.status, r.ota_ref) for r in res], [("Confirmed", rev["ota_reservation_code"])])
		self.assertEqual(self.fake.acks(), [rev["id"]])
		ack_at = self.order.index(("ack", rev["id"]))
		self.assertIn(("commit", 1), self.order[:ack_at], self.order)
		row = self.row(rev)
		self.assertEqual(row.status, "Acked")
		self.assertTrue(row.applied_at and row.acked_at)
		self.assertEqual(row.booking_id, rev["booking_id"])
		self.assertEqual(json.loads(row.results)[0]["result"], "booked")
		# the feed is empty now: nothing more to do
		self.assertEqual(self.pull(), {"acked": 0, "failed": 0, "skipped": 0})
		self.assertEqual(self.fake.acks(), [rev["id"]])

	def test_redelivered_revision_is_applied_once_and_acked_again(self):
		rev = self.rev()
		self.fake.serve(rev)
		self.fake.fail("POST", f"booking_revisions/{rev['id']}/ack", channex.ChannexError(500, "boom"))
		with patch.object(cm, "_apply_event", wraps=cm._apply_event) as spy:
			self.pull()
			self.assertEqual(self.row(rev).status, "Applied")
			self.pull()
		self.assertEqual(spy.call_count, 1)
		self.assertEqual(len(self.reservations(rev["ota_reservation_code"])), 1)
		self.assertEqual(self.fake.acks(), [rev["id"], rev["id"]])
		self.assertEqual(self.row(rev).status, "Acked")
		self.assertEqual(self.row(rev).attempts, 2)

	def test_failed_revision_is_kept_unacked_with_a_clean_error_log(self):
		rev = self.rev(phone="+598 99 765 432")
		self.fake.serve(rev)
		before = frappe.db.count("Error Log")
		with patch.object(cm, "_apply_event", side_effect=RuntimeError("boom")):
			out = self.pull()
		self.assertEqual(out, {"acked": 0, "failed": 1, "skipped": 0})
		self.assertEqual(self.fake.acks(), [])
		row = self.row(rev)
		self.assertEqual((row.status, row.attempts), ("Failed", 1))
		self.assertIn("RuntimeError", row.last_error)
		logs = frappe.get_all("Error Log", filters={"method": ("like", "Channex revision%")},
			fields=["method", "error"], order_by="creation desc", limit=1)
		self.assertGreater(frappe.db.count("Error Log"), before)
		self.assertTrue(logs and logs[0].method.startswith("Channex revision"), logs)
		self.assertIn(rev["id"], logs[0].method)
		for secret in ("765 432", "765432", rev["customer"]["mail"], "4111111111111111", "Prueba"):
			self.assertNotIn(secret, logs[0].error)
		# next pass, without the failure: applied and acked
		self.assertEqual(self.pull(), {"acked": 1, "failed": 0, "skipped": 0})
		self.assertEqual(len(self.reservations(rev["ota_reservation_code"])), 1)
		self.assertEqual(self.row(rev).status, "Acked")

	def test_sold_out_revision_is_requested_and_acked(self):
		self.sold_out()
		rev = self.rev()
		self.fake.serve(rev)
		self.assertEqual(self.pull(), {"acked": 1, "failed": 0, "skipped": 0})
		res = self.reservations(rev["ota_reservation_code"])
		self.assertEqual([r.status for r in res], ["Requested"])
		self.assertIn("OTA overbooking — resolve", res[0].special_requests)
		self.assertEqual(self.fake.acks(), [rev["id"]])

	def test_unmapped_room_is_a_failure_never_acked(self):
		rev = self.rev()
		rev["rooms"][0]["room_type_id"] = "rt-nobody-mapped"
		self.fake.serve(rev)
		self.assertEqual(self.pull(), {"acked": 0, "failed": 1, "skipped": 0})
		self.assertEqual(self.reservations(rev["ota_reservation_code"]), [])
		self.assertEqual(self.fake.acks(), [])
		self.assertEqual(self.row(rev).status, "Failed")
		self.assertIn("rt-nobody-mapped", self.row(rev).last_error)

	def test_new_modified_cancelled_in_one_pass_in_order(self):
		code = "UT-SEQ-" + frappe.generate_hash(length=6)
		new, mod, cancel = self.rev(code=code), self.rev(code=code, status="modified", nights=2), \
			self.rev(code=code, status="cancelled")
		for r in (new, mod, cancel):
			self.fake.serve(r)
		self.assertEqual(self.pull(), {"acked": 3, "failed": 0, "skipped": 0})
		res = self.reservations(code)
		self.assertEqual([r.status for r in res], ["Cancelled"])
		self.assertEqual(str(res[0].check_out_date), str(add_days(nowdate(), 2)))
		self.assertEqual(self.fake.acks(), [new["id"], mod["id"], cancel["id"]])

	def test_a_failed_modify_holds_back_the_later_cancel(self):
		code = "UT-HOLD-" + frappe.generate_hash(length=6)
		new, mod, cancel = self.rev(code=code), self.rev(code=code, status="modified", nights=2), \
			self.rev(code=code, status="cancelled")
		other = self.rev()
		for r in (new, mod, cancel, other):
			self.fake.serve(r)
		real = cm._apply_event

		def flaky(conn, e, *a, **k):
			if e["event"] == "modify":
				raise RuntimeError("modify failed")
			return real(conn, e, *a, **k)

		with patch.object(cm, "_apply_event", side_effect=flaky):
			out = self.pull()
		self.assertEqual(out, {"acked": 2, "failed": 1, "skipped": 1})
		self.assertEqual(self.fake.acks(), [new["id"], other["id"]])
		self.assertEqual([r.status for r in self.reservations(code)], ["Confirmed"])
		self.assertFalse(frappe.db.exists("Channel Booking Revision", cancel["id"]))
		# fixed: modify, then cancel, in order
		self.assertEqual(self.pull(), {"acked": 2, "failed": 0, "skipped": 0})
		self.assertEqual(self.fake.acks()[2:], [mod["id"], cancel["id"]])
		self.assertEqual([r.status for r in self.reservations(code)], ["Cancelled"])

	def test_two_rooms_are_two_reservations_and_one_cancel_reaches_both(self):
		code = "UT-TWO-" + frappe.generate_hash(length=6)
		room = self.rev()["rooms"][0]
		two = self.rev(code=code, amount="200.00", ota_commission="30.00",
			rooms=[dict(room, amount="100.00"), dict(room, amount="100.00")])
		self.fake.serve(two)
		self.assertEqual(self.pull(), {"acked": 1, "failed": 0, "skipped": 0})
		self.assertEqual([(r.ota_ref, r.status) for r in self.reservations(code)],
			[(code + "-0", "Confirmed"), (code + "-1", "Confirmed")])
		self.fake.serve(self.rev(code=code, status="cancelled"))
		self.assertEqual(self.pull(), {"acked": 1, "failed": 0, "skipped": 0})
		self.assertEqual([r.status for r in self.reservations(code)], ["Cancelled", "Cancelled"])

	def test_rate_limit_pauses_the_connection(self):
		self.fake.serve(self.rev())
		self.fake.fail("GET", "booking_revisions/feed", channex.ChannexRateLimited(429, "slow down", retry_after=30))
		self.assertEqual(self.pull(), {"paused": 30})
		calls = len(self.fake.calls)
		out = self.pull()
		self.assertIn("paused", out)
		self.assertGreater(out["paused"], 0)
		self.assertEqual(len(self.fake.calls), calls)
		self.assertEqual(self.fake.acks(), [])

	def test_feed_failure_logs_once_an_hour(self):
		self.fake.fail("GET", "booking_revisions/feed", channex.ChannexError(401, "unauthorized"), times=2)
		before = frappe.db.count("Error Log", {"method": f"Channex feed {self.conn.name}"})
		self.pull()
		self.pull()
		self.assertEqual(frappe.db.count("Error Log", {"method": f"Channex feed {self.conn.name}"}), before + 1)

	def test_refused_phone_keeps_the_booking_and_flags_the_contact(self):
		rev = self.rev(phone="abc-123")
		self.fake.serve(rev)
		self.assertEqual(self.pull(), {"acked": 1, "failed": 0, "skipped": 0})
		res = self.reservations(rev["ota_reservation_code"])
		self.assertEqual(len(res), 1)
		self.assertEqual(frappe.db.get_value("Guest", res[0].guest, "phone") or "", "")
		self.assertIn("Contact from", res[0].special_requests)
		self.assertIn("abc-123", res[0].special_requests)

	def test_cron_enqueues_one_pull_per_active_channex_connection(self):
		inactive = frappe.get_doc({"doctype": "Channel Manager Connection", "property": self.property,
			"provider": "Channex", "active": 0, "external_property_id": "prop-off"}).insert(ignore_permissions=True)
		aiosell = frappe.get_doc({"doctype": "Channel Manager Connection", "property": self.property,
			"provider": "AioSell", "active": 1, "external_property_id": "aio"}).insert(ignore_permissions=True)
		with patch("frappe.enqueue") as enqueue:
			cm.enqueue_channex_pulls()
		jobs = {c.kwargs.get("job_id"): c for c in enqueue.call_args_list}
		self.assertIn(f"channex-feed::{self.conn.name}", jobs)
		self.assertNotIn(f"channex-feed::{inactive.name}", jobs)
		self.assertNotIn(f"channex-feed::{aiosell.name}", jobs)
		call = jobs[f"channex-feed::{self.conn.name}"]
		self.assertEqual(call.args[0], "kamra.channel_manager.pull_feed")
		self.assertEqual(call.kwargs["queue"], "short")
		self.assertEqual(call.kwargs["connection"], self.conn.name)
		self.assertTrue(call.kwargs["deduplicate"])

	def test_revision_row_holds_no_guest_data(self):
		rev = self.rev()
		self.fake.serve(rev)
		self.pull()
		values = " ".join(str(v) for v in self.row(rev).as_dict().values())
		for secret in ("Ana", "Prueba", "765 432", rev["customer"]["mail"], "4111", "Quiet room"):
			self.assertNotIn(secret, values)


class ChannexWebhookTestCase(_ChannexConnectionMixin, IntegrationTestCase):
	"""The webhook is a signal: authenticate, enqueue the feed pull, answer."""

	def post(self, body, headers=None, connection=True, raw=None):
		"""post_webhook of test_channel_manager_bookings, with the headers and
		the raw body in the test's hands (the HMAC signs the raw body)."""
		from werkzeug.test import EnvironBuilder
		from frappe.app import make_form_dict
		data = raw if raw is not None else json.dumps(body).encode()
		request = EnvironBuilder(method="POST", path="/api/method/kamra.channel_manager.webhook",
			query_string={"connection": self.conn.name} if connection else None, data=data,
			content_type="application/json", headers=headers or {}).get_request()
		before = getattr(frappe.local, "request", None), frappe.local.form_dict
		frappe.local.request = request
		make_form_dict(request)
		frappe.set_user("Guest")  # nosemgrep: frappe-setuser -- test fixture: the endpoint's real caller
		try:
			return frappe.call("kamra.channel_manager.webhook", **frappe.form_dict)
		finally:
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
			frappe.local.request, frappe.local.form_dict = before

	def signal(self, event="booking", property_id="prop-ut"):
		return {"event": event, "payload": {"booking_id": "b-1", "property_id": property_id,
			"revision_id": "r-1"}, "property_id": property_id, "user_id": None,
			"timestamp": "2026-11-16T00:00:00Z"}

	def test_booking_signal_only_enqueues_and_answers_at_once(self):
		with patch("frappe.enqueue") as enqueue, patch.object(cm, "_apply_event") as apply:
			start = time.monotonic()
			out = self.post(self.signal(), {"X-Webhook-Secret": "ut-secret"})
			elapsed = time.monotonic() - start
		self.assertEqual(out, {"ok": True, "queued": True})
		self.assertEqual(enqueue.call_count, 1)
		self.assertEqual(enqueue.call_args.args[0], "kamra.channel_manager.pull_feed")
		self.assertEqual(enqueue.call_args.kwargs["job_id"], f"channex-feed::{self.conn.name}")
		self.assertTrue(enqueue.call_args.kwargs["deduplicate"])
		self.assertEqual(enqueue.call_args.kwargs["queue"], "short")
		apply.assert_not_called()
		self.assertLess(elapsed, 1.0)

	def test_every_booking_event_enqueues(self):
		for event in ("booking_new", "booking_modification", "booking_cancellation", "non_acked_booking"):
			with patch("frappe.enqueue") as enqueue:
				out = self.post(self.signal(event), {"X-Webhook-Secret": "ut-secret"})
			self.assertEqual(out, {"ok": True, "queued": True}, event)
			self.assertEqual(enqueue.call_count, 1, event)

	def test_wrong_secret_is_refused_and_nothing_queued(self):
		with patch("frappe.enqueue") as enqueue:
			with self.assertRaises(frappe.PermissionError):
				self.post(self.signal(), {"X-Webhook-Secret": "wrong"})
		enqueue.assert_not_called()

	def test_missing_connection_is_a_validation_error(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.post(self.signal(), {"X-Webhook-Secret": "ut-secret"}, connection=False)
		self.assertNotIsInstance(ctx.exception, frappe.DoesNotExistError)
		self.assertIn("Missing connection", str(ctx.exception))

	def test_other_events_are_ignored(self):
		with patch("frappe.enqueue") as enqueue:
			out = self.post(self.signal("ari"), {"X-Webhook-Secret": "ut-secret"})
		self.assertEqual(out, {"ok": True, "ignored": "ari"})
		enqueue.assert_not_called()

	def test_another_property_is_ignored(self):
		with patch("frappe.enqueue") as enqueue:
			out = self.post(self.signal(property_id="someone-else"), {"X-Webhook-Secret": "ut-secret"})
		self.assertEqual(out, {"ok": True, "ignored": "other_property"})
		enqueue.assert_not_called()

	def test_unmapped_room_event_enqueues_and_logs_without_guest_data(self):
		body = self.signal("booking_unmapped_room")
		body["payload"] = {"booking_revision_id": "r-1", "property_id": "prop-ut",
			"customer": {"name": "Ana", "mail": "ana-unmapped@example.test"}}
		with patch("frappe.enqueue") as enqueue:
			out = self.post(body, {"X-Webhook-Secret": "ut-secret"})
		self.assertEqual(out, {"ok": True, "queued": True})
		self.assertEqual(enqueue.call_count, 1)
		logs = frappe.get_all("Error Log", filters={"method": f"Channex unmapped {self.conn.name}"},
			fields=["error"], order_by="creation desc", limit=1)
		self.assertTrue(logs)
		self.assertNotIn("ana-unmapped@example.test", logs[0].error)

	def signed(self, secret, raw, ts=None):
		ts = ts or str(int(time.time()))
		sig = hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
		return {"x-hmac-timestamp": ts, "x-hmac-signature-256": sig, "x-hmac-keys-256": "k1"}

	def test_signing_secret_requires_a_valid_hmac(self):
		self.conn.webhook_signing_secret = "sign-secret"
		self.conn.save(ignore_permissions=True)
		raw = json.dumps(self.signal()).encode()
		with patch("frappe.enqueue") as enqueue:
			out = self.post(None, self.signed("sign-secret", raw), raw=raw)
		self.assertEqual(out, {"ok": True, "queued": True})
		self.assertEqual(enqueue.call_count, 1)
		for headers in (dict(self.signed("other-secret", raw), **{"X-Webhook-Secret": "ut-secret"}),
		                {"X-Webhook-Secret": "ut-secret"}):
			with patch("frappe.enqueue") as enqueue:
				with self.assertRaises(frappe.PermissionError):
					self.post(None, headers, raw=raw)
			enqueue.assert_not_called()

	def test_cron_entry_runs_every_minute(self):
		cron = frappe.get_hooks("scheduler_events")["cron"]
		self.assertIn("kamra.channel_manager.enqueue_channex_pulls", cron["* * * * *"])
