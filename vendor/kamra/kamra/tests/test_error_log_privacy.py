"""An Error Log never keeps a guest's data (HR-139, after HR-76).

frappe.log_error with no message stores the traceback WITH every frame's
local variables on a developer-mode site (the booking, the guest, the
payload), and on any site the error text itself may quote the guest (an
email Frappe refused, a phone the provider rejected). So:

1. every frappe.log_error in Kamra passes a message - the census below
   reads the source and fails on a new one without it;
2. where the error can be about a guest (check-in, ID documents, WhatsApp,
   folios, reservations, the night audit, banquets, outbound integrations)
   the message is kamra.privacy.guest_traceback: the trace without local
   variables and without the guest's name, email, phone or ID number.

Each family forces its failure with an error that quotes the guest, with
developer_mode off and on, and reads the Error Log row."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import frappe

import kamra
from kamra import api, booking_requests, checkin, id_documents, localization, night_audit
from kamra import folio as folio_mod
from kamra import privacy, savings, webhooks, whatsapp
from kamra.banquet_ops import maybe_email_guest_quote_chase
from kamra.kamra.doctype.reservation.reservation import Reservation
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	AuditTestCase,
	d,
	guest_folio,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)

FIRST, LAST = "Lucía", "Fernández"
EMAIL = "lucia.fernandez@example.test"
PHONE = "+598 99 123 456"
ID_NUMBER = "4.567.890-1"
# what an error may say about her, in the writings a person or a provider uses
QUOTE = (f"{FIRST} {LAST} ({EMAIL}, +59899123456, 099 123 456, CI {ID_NUMBER})"
         " was refused")
SECRETS = (FIRST, LAST, "lucia.fernandez", "example.test", "123 456", "99123456",
           "099 123", "4.567.890")


# --------------------------------------------------------------- the census

def log_errors_without_message(source: str, filename: str = "<src>") -> list[int]:
	"""Lines of the frappe.log_error calls in `source` that pass no message:
	no second positional argument and no message= (a **kwargs call counts as
	none - the census cannot see inside it)."""
	lines = []
	for node in ast.walk(ast.parse(source, filename)):
		if not isinstance(node, ast.Call):
			continue
		func = node.func
		if not ((isinstance(func, ast.Attribute) and func.attr == "log_error")
		        or (isinstance(func, ast.Name) and func.id == "log_error")):
			continue
		if len(node.args) >= 2 or any(k.arg == "message" for k in node.keywords):
			continue
		lines.append(node.lineno)
	return lines


def kamra_sources():
	"""Every .py of the kamra app outside its tests."""
	root = Path(kamra.__file__).parent
	for path in sorted(root.rglob("*.py")):
		rel = path.relative_to(root)
		if "tests" in rel.parts or rel.name.startswith("test_"):
			continue
		yield rel, path


class ErrorLogCensusTestCase(unittest.TestCase):
	def test_every_log_error_passes_a_message(self):
		missing = [f"{rel}:{line}" for rel, path in kamra_sources()
		           for line in log_errors_without_message(path.read_text(), str(path))]
		self.assertGreater(len(list(kamra_sources())), 100, "the census found no Kamra sources")
		self.assertEqual(missing, [], "frappe.log_error without a message stores the local "
		                 "variables on a developer-mode site: pass message= (for a guest's "
		                 "record, kamra.privacy.guest_traceback(...))")

	def test_the_census_sees_every_form_of_a_call_without_message(self):
		without = (
			'frappe.log_error(title="x")',
			'frappe.log_error("x")',
			'frappe.log_error()',
			'log_error(title=f"x {y}", reference_doctype="Reservation")',
			'frappe.log_error(**kw)',
		)
		for src in without:
			with self.subTest(src=src):
				self.assertEqual(log_errors_without_message(src), [1])
		for src in ('frappe.log_error(title="x", message=m)', 'frappe.log_error("x", m)',
		            'frappe.log_error(message=m)', 'frappe.logger().error("x")'):
			with self.subTest(src=src):
				self.assertEqual(log_errors_without_message(src), [])


# ------------------------------------------------------------- the families

def _boom(*args, **kwargs):
	raise RuntimeError(QUOTE)


class _Lock:
	"""A lock system whose answer quotes the guest it was given."""
	key = "ut-lock"

	supports_revoke = True

	def issue_key(self, req):
		raise RuntimeError(QUOTE)

	def revoke_keys(self, property, reservation, room):
		raise RuntimeError(QUOTE)


class ErrorLogPrivacyTestCase(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY)
		self.rt = make_room_type(PROP_A, "STD", base_price=1000)
		self.room = make_room(PROP_A, self.rt, "101")
		self.guest = frappe.get_doc({"doctype": "Guest", "first_name": FIRST, "last_name": LAST,
			"email": EMAIL, "phone": PHONE, "id_number": ID_NUMBER}).insert(ignore_permissions=True).name
		self.res = make_reservation(PROP_A, self.rt, d(-1), d(1), status="Checked In",
		                            room=self.room, guest=self.guest)
		self.folio = guest_folio(self.res.name)

	def assert_clean_log(self, title: str, trigger, secrets: tuple = SECRETS):
		"""`trigger` fails and logs `title` once per run; with developer_mode
		off and on, the row carries the trace and nothing of the guest."""
		for developer_mode in (0, 1):
			with self.subTest(title=title, developer_mode=developer_mode), \
			     patch.dict(frappe.conf, {"developer_mode": developer_mode}):
				before = frappe.db.count("Error Log", {"method": title})
				trigger()
				self.assertEqual(frappe.db.count("Error Log", {"method": title}), before + 1)
				error = frappe.get_all("Error Log", filters={"method": title}, fields=["error"],
				                       order_by="creation desc", limit=1)[0].error
				self.assertIn("Traceback", error)
				self.assertIn("RuntimeError", error)
				for secret in secrets:
					self.assertNotIn(secret, error)

	# ---- the helper

	def test_guests_of_a_stay_names_its_guest_booker_and_occupants(self):
		self.res.booked_by_name = "Martín Suárez"
		self.res.booked_by_phone = "+598 98 765 432"
		self.res.append("occupants", {"full_name": "Sofía Pérez", "phone": "+598 97 111 222",
		                              "id_number": "1.234.567-8"})
		self.res.save(ignore_permissions=True)
		for records in ((self.res,), (("Reservation", self.res.name),),
		                (("Folio", self.folio),), (("Reservation", [self.res.name]),)):
			with self.subTest(records=records):
				text = privacy._scrub_guest(
					f"{QUOTE}; booked by Martín Suárez +59898765432; with Sofía Pérez "
					"097 111 222 (1.234.567-8)", privacy.guests_of(*records))
				for secret in (*SECRETS, "Martín", "Suárez", "98765432", "Sofía", "Pérez",
				               "111 222", "1.234.567-8"):
					self.assertNotIn(secret, text)
				self.assertIn("[redacted]", text)

	def test_the_stays_of_a_day_are_the_guests_of_a_whole_property(self):
		text = privacy._scrub_guest(QUOTE, privacy.guests_of(stays=(PROP_A, TODAY)))
		for secret in SECRETS:
			self.assertNotIn(secret, text)

	def test_a_failed_lookup_withholds_the_error_text(self):
		"""The guest cannot be looked up: the trace stays, the error texts go
		- of a chained error too."""
		def chained():
			try:
				_boom()
			except RuntimeError as exc:
				raise frappe.ValidationError(f"while billing {EMAIL}") from exc

		for fails, kinds in ((_boom, ("RuntimeError",)),
		                     (chained, ("RuntimeError", "ValidationError", "direct cause"))):
			with self.subTest(fails=fails.__name__), \
			     patch.object(privacy, "guests_of", side_effect=RuntimeError("database gone")):
				try:
					fails()
				except Exception:
					text = privacy.guest_traceback(self.res)
				self.assertIn("Traceback", text)
				self.assertIn("_boom", text)
				for kind in kinds:
					self.assertIn(kind, text)
				self.assertNotIn("database gone", text)
				for secret in SECRETS:
					self.assertNotIn(secret, text)

	# ---- check-in

	def test_check_in_a_lock_that_fails_on_the_key(self):
		self.assert_clean_log("Door lock ut-lock issue_key failed",
		                      lambda: checkin._call_adapter(_Lock(), self.res, 2))

	def test_undoing_a_check_in_a_lock_that_fails_on_revoking_the_keys(self):
		"""Kamra 2.7.10 cancels the keys when a check-in is undone
		(api._revoke_stay_keys); a lock whose answer quotes her (HR-183)."""
		with patch("kamra.door_lock.get_adapter", return_value=_Lock()):
			self.assert_clean_log("Door lock ut-lock revoke_keys failed",
			                      lambda: api._revoke_stay_keys(self.res, [self.room]))

	# ---- ID documents

	def test_id_documents_a_scan_that_cannot_be_removed(self):
		with patch.object(id_documents, "_existing", return_value=["ut-ghost-file"]), \
		     patch("frappe.delete_doc", side_effect=_boom):
			self.assert_clean_log(f"ID document discard failed: {self.res.name}",
			                      lambda: id_documents.discard_id_document(self.res.name))
			with patch.object(id_documents, "_decode", return_value=b"raw"), \
			     patch.object(id_documents, "_sanitise", return_value=b"jpeg"), \
			     patch("frappe.utils.file_manager.save_file",
			           return_value=SimpleNamespace(file_url="/private/files/id-ut.jpg")):
				self.assert_clean_log(f"ID document: stale file kept ({self.res.name})",
				                      lambda: id_documents.store_id_document(self.res, "x", "Desk"))

	# ---- WhatsApp

	def test_whatsapp_messages_that_fail(self):
		with patch("kamra.payments.create_payment_link", side_effect=_boom):
			self.assert_clean_log("WhatsApp payment link failed",
			                      lambda: whatsapp._payment_link(self.res, 100))
		folio = frappe.get_doc("Folio", self.folio)
		with patch("kamra.settings.get_setting", side_effect=_boom):
			self.assert_clean_log("WhatsApp room charge alert not queued",
			                      lambda: whatsapp.queue_room_charge_alert(folio, {
				                      "charge_type": "Food & Beverage", "amount": 100,
				                      "reservation": self.res.name}))
		frappe.db.set_value("Property", PROP_A, "auto_send_checkin_links", 1)
		# since Kamra 2.7.10 a property with no WhatsApp line is skipped (HR-183)
		frappe.get_doc({"doctype": "Channel Provider Connection", "property": PROP_A,
		                "channel": "WhatsApp", "provider": "Custom",
		                "active": 1}).insert(ignore_permissions=True)
		arrival = make_reservation(PROP_A, self.rt, d(1), d(2), guest=self.guest)
		from kamra import prearrival
		with patch("kamra.api.send_precheckin_link", side_effect=_boom), \
		     patch("frappe.utils.nowdate", return_value=TODAY):
			self.assert_clean_log(f"Pre-arrival outreach failed for {arrival.name}",
			                      lambda: prearrival.run_prearrival_outreach(horizon_days=3))

	# ---- folios

	def test_folio_postings_whose_ledger_write_fails(self):
		with patch("kamra.ledger.record_charge_ledger", side_effect=_boom):
			self.assert_clean_log("ledger charge write failed",
			                      lambda: api.add_folio_charge(self.folio, "Food & Beverage",
			                                                   "UT dinner", 100, gst_rate=0))
		with patch("kamra.folio.post_remaining_nights", side_effect=_boom):
			self.assert_clean_log("Kamra: check-out balance projection",
			                      lambda: api._checkout_projection([self.res.name]))

	def test_a_void_whose_reversal_cannot_reach_the_ledger_fails_whole(self):
		"""Since Kamra 2.7.10 (22321a9c3, HR-183) a void keeps the line and posts
		a signed reversal line, and its ledger write is no longer caught into
		an Error Log ("ledger void reversal failed" is gone): the void fails
		whole, and nothing about her is logged."""
		api.add_folio_charge(self.folio, "Food & Beverage", "UT dinner", 100, gst_rate=0)
		row = frappe.get_doc("Folio", self.folio).charges[-1].name
		before = frappe.db.count("Error Log")
		with patch("kamra.ledger.record_charge_ledger", side_effect=_boom), \
		     self.assertRaises(RuntimeError):
			folio_mod.void_charge(self.folio, row, "UT wrong line")
		self.assertEqual(frappe.db.count("Error Log"), before)

	# ---- reservations

	def _stay(self, **fields):
		"""A new stay of hers on nights nobody else holds (one room)."""
		self._offset = getattr(self, "_offset", 10) + 2
		return make_reservation(PROP_A, self.rt, d(self._offset), d(self._offset + 1),
		                        guest=self.guest, **fields)

	def test_reservation_steps_that_fail(self):
		with patch("kamra.deposit.ensure_deposit_for_reservation", side_effect=_boom):
			self.assert_clean_log("Security deposit ensure failed", self._stay)
			self.assert_clean_log("Security deposit ensure on confirm failed",
			                      lambda: api.confirm_pending_reservation(
				                      self._stay(status="Held").name))
		with patch.object(booking_requests, "_email_ready", return_value=True), \
		     patch("frappe.sendmail", side_effect=_boom):
			self.assert_clean_log(f"Booking request email failed: {self.res.name}",
			                      lambda: booking_requests._notify_guest(self.res, accepted=True))
		held = self._stay(status="Held")
		frappe.db.set_value("Reservation", held.name, "hold_expires_on",
		                    frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-1))
		from kamra import reservation_state
		with patch.object(Reservation, "save", side_effect=_boom):
			self.assert_clean_log(f"Hold expiry failed for {held.name}",
			                      reservation_state.expire_holds)

	def test_a_bell_line_that_cannot_be_written(self):
		"""Kamra's notification bell (2.7.6, HR-183): its subject names the
		guest ("Checked in - Lucía Fernández"), so neither the title nor the
		trace of a line that can't be written may carry it."""
		from kamra import notifications
		from kamra.kamra.doctype.kamra_notification.kamra_notification import KamraNotification
		with patch.object(KamraNotification, "before_insert", side_effect=_boom, create=True):
			self.assert_clean_log("Notification not recorded: front-desk",
			                      lambda: notifications.record(
				                      PROP_A, "front-desk", f"Checked in - {FIRST} {LAST}",
				                      doctype="Reservation", name=self.res.name))

	def test_an_action_log_row_that_cannot_be_written(self):
		row = {"action_type": "check_in", "rationale": f"Checked in {FIRST} {LAST}, {PHONE}",
		       "reference_doctype": "Reservation", "reference_name": self.res.name}

		def trigger():
			try:
				_boom()
			except RuntimeError as exc:
				savings._report_failure("check_in", row, exc)
		self.assert_clean_log("Agent Action Log write failed: check_in", trigger)

	# ---- the night audit

	def test_night_audit_steps_that_fail(self):
		pack = {"property": PROP_A, "business_date": TODAY}
		self.assert_clean_log("Night audit pack section failed: ut_section",
		                      lambda: night_audit._section(pack, "ut_section", _boom))
		with patch("kamra.settings.get_setting", side_effect=_boom):
			self.assert_clean_log(f"Night audit failed: {PROP_A}",
			                      folio_mod.nightly_audit_all_properties)

	# ---- banquets

	def test_banquet_quote_chase_email_that_fails(self):
		booking = frappe.get_doc({"doctype": "Venue Booking", "property": PROP_A,
			"customer_name": f"{FIRST} {LAST}", "customer_email": EMAIL, "customer_phone": PHONE,
			"event_date": TODAY})
		booking.name = "UT-VB-HR139"
		# a banquet customer leaves a name, an email and a phone - no ID number
		with patch("frappe.sendmail", side_effect=_boom):
			self.assert_clean_log("banquet quote chase email",
			                      lambda: maybe_email_guest_quote_chase(booking, {}),
			                      secrets=SECRETS[:-1])

	# ---- outbound integrations

	def test_webhooks_and_e_invoicing_that_fail(self):
		with patch.object(webhooks, "_subscribers", side_effect=_boom):
			self.assert_clean_log("Webhook emit failed: reservation.created",
			                      lambda: webhooks.emit("reservation.created", PROP_A,
			                                            webhooks.reservation_payload(self.res)))
		pack = SimpleNamespace(on_invoice_issued=_boom)
		self.assert_clean_log("e-invoicing: on_invoice_issued failed",
		                      lambda: localization.on_invoice_issued(pack, self.folio))
