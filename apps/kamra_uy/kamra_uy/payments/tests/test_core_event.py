"""Los avisos firmados de core en el folio (HR-30): cobro, devolución y contracargo, una vez por aviso y por pago.

Lo corre `bench run-tests --app kamra_uy` (o `--module kamra_uy.payments.tests.test_core_event`).
"""

import json
import quopri
import uuid
from unittest.mock import patch

import frappe
from frappe.utils import now_datetime

from kamra import payments as kamra_payments
from kamra.tests.audit_fixtures import ensure_user
from kamra_uy.payments import texts, webhook
from kamra_uy.payments.tests.fixtures import (
	CODE,
	FINANCE,
	OTHER,
	OTHER_CODE,
	OTHER_SECRET,
	PROP,
	CobrosCase,
	event,
	signed,
)

PAY_1 = "7a2b0c11-1111-4222-8333-555555555555"
PAY_2 = "7a2b0c11-1111-4222-8333-999999999999"


class TestCoreEvent(CobrosCase):
	def receive(self, body: dict, **sign_over) -> dict:
		raw, headers = signed(body, **sign_over)
		return webhook.receive(raw, headers)

	def stored(self, body: dict):
		return frappe.get_doc("UY Core Event", body["event_id"])

	def mails(self) -> int:
		return frappe.db.count("Email Queue", {"reference_doctype": "UY Core Event"})

	# --- cobro --------------------------------------------------------------

	def test_an_approved_payment_posts_once_with_its_mode_and_confirms_the_hold(self):
		res, folio = self.booking("Held")
		body = event("payment.approved", folio, mode="Tarjeta extranjera", card_country="AR")
		self.assertEqual(self.receive(body)["status"], "Applied")
		(row,) = self.rows(folio)
		self.assertEqual((row.mode, row.reference, row.payment_kind, row.amount),
		                 ("Tarjeta extranjera", PAY_1, "Advance", 1250.5))
		self.assertEqual(frappe.db.get_value("Reservation", res, "status"), "Confirmed")
		self.assertEqual(float(frappe.db.get_value("Reservation", res, "advance_paid")), 1250.5)
		stored = self.stored(body)
		self.assertEqual((stored.status, stored.event_type, stored.property, stored.folio, stored.payment_id,
		                  stored.core_property), ("Applied", "payment.approved", PROP, folio, PAY_1, CODE))

	def test_the_same_event_twice_is_a_duplicate(self):
		_, folio = self.booking()
		body = event("payment.approved", folio)
		self.receive(body)
		self.assertEqual(self.receive(body), {"duplicate": True})
		self.assertEqual(len(self.rows(folio)), 1)

	def test_two_payments_of_one_link_are_two_rows(self):
		_, folio = self.booking()
		self.receive(event("payment.approved", folio))
		self.receive(event("payment.approved", folio, payment_id=PAY_2, amount="100.00"))
		self.assertEqual(sorted(r.reference for r in self.rows(folio)), sorted([PAY_1, PAY_2]))

	def test_another_event_for_a_payment_already_posted_is_a_duplicate(self):
		_, folio = self.booking()
		self.receive(event("payment.approved", folio))
		again = event("payment.approved", folio)
		self.assertEqual(self.receive(again)["status"], "Duplicate")
		self.assertEqual(len(self.rows(folio)), 1)

	def test_without_the_card_country_it_is_a_payment_link(self):
		_, folio = self.booking()
		self.receive(event("payment.approved", folio, card_country=None, mode="Payment Link"))
		self.assertEqual(self.rows(folio)[0].mode, "Payment Link")

	# --- firma y propiedad --------------------------------------------------

	def test_a_bad_signature_is_refused_and_posts_nothing(self):
		_, folio = self.booking()
		body = event("payment.approved", folio)
		with self.assertRaises(frappe.PermissionError):
			self.receive(body, secret="not-the-secret")
		self.assertEqual(self.rows(folio), [])
		self.assertFalse(frappe.db.exists("UY Core Event", body["event_id"]))

	def test_a_stale_signature_is_refused(self):
		_, folio = self.booking()
		with self.assertRaises(frappe.PermissionError):
			self.receive(event("payment.approved", folio), t=1)

	def test_another_property_cannot_post_on_this_folio(self):
		_, folio = self.booking()
		for kind in ("payment.approved", "payment.refunded", "payment.charged_back"):
			with self.subTest(kind=kind), self.assertRaises(frappe.PermissionError):
				self.receive(event(kind, folio, property=OTHER_CODE), secret=OTHER_SECRET)
		self.assertEqual(self.rows(folio), [])

	def test_an_unknown_property_is_refused(self):
		_, folio = self.booking()
		with self.assertRaises(frappe.PermissionError):
			self.receive(event("payment.approved", folio, property="HR-NADIE"))

	def test_a_changed_event_id_header_is_refused(self):
		_, folio = self.booking()
		with self.assertRaises(frappe.PermissionError):
			self.receive(event("payment.approved", folio), event_id="otro-id")

	def test_a_body_that_is_not_an_event_is_a_400(self):
		with self.assertRaises(webhook.InvalidEvent) as err:
			webhook.receive(b'{"type": "cfe.accepted"}', {})
		self.assertEqual(err.exception.http_status_code, 400)

	# --- devolución y contracargo ------------------------------------------

	def test_a_partial_refund_posts_once(self):
		_, folio = self.booking()
		self.receive(event("payment.approved", folio, mode="Tarjeta local"))
		body = event("payment.refunded", folio, amount="100.00")
		self.assertEqual(self.receive(body)["status"], "Applied")
		self.assertEqual(self.receive(body), {"duplicate": True})
		again = event("payment.refunded", folio, amount="100.00", refund_id=body["refund_id"])
		self.assertEqual(self.receive(again)["status"], "Duplicate")
		refunds = [r for r in self.rows(folio) if r.payment_kind == "Refund"]
		self.assertEqual([(r.amount, r.reference, r.mode) for r in refunds],
		                 [(-100.0, f"refund:{PAY_1}:{body['refund_id']}", "Tarjeta local")])

	def test_a_total_refund_returns_the_whole_payment(self):
		res, folio = self.booking()
		self.receive(event("payment.approved", folio))
		self.receive(event("payment.refunded", folio, amount="1250.50"))
		self.assertAlmostEqual(kamra_payments.refunded_for_payment(frappe.get_doc("Folio", folio), PAY_1), 1250.5)
		self.assertEqual(float(frappe.db.get_value("Reservation", res, "advance_paid")), 0)

	def test_a_chargeback_after_a_partial_refund_takes_only_the_rest_and_needs_a_person(self):
		self.outbox()
		_, folio = self.booking()
		self.receive(event("payment.approved", folio))
		self.receive(event("payment.refunded", folio, amount="100.00"))
		body = event("payment.charged_back", folio, amount="1250.50")
		self.assertEqual(self.receive(body)["status"], "Needs attention")
		chargeback = [r for r in self.rows(folio) if r.reference == f"chargeback:{PAY_1}"]
		self.assertEqual([r.amount for r in chargeback], [-1150.5])
		self.assertEqual(self.stored(body).status, "Needs attention")
		self.assertEqual(self.mails(), 1)

	def test_a_chargeback_of_a_payment_already_refunded_posts_nothing(self):
		_, folio = self.booking()
		self.receive(event("payment.approved", folio))
		self.receive(event("payment.refunded", folio, amount="1250.50"))
		body = event("payment.charged_back", folio, amount="1250.50")
		self.assertEqual(self.receive(body)["status"], "Needs attention")
		self.assertFalse([r for r in self.rows(folio) if r.reference.startswith("chargeback:")])

	# --- el pago que llega tarde (HR-31) ----------------------------------------

	def expired(self) -> tuple[str, str]:
		"""Una reserva web cuya retención venció: Kamra la canceló y liberó la habitación."""
		from frappe.utils import add_to_date, now_datetime

		from kamra.reservation_state import HOLD_EXPIRY_NOTE, expire_holds

		res, folio = self.booking("Pending Payment", hold_expires_on=add_to_date(now_datetime(), minutes=30))
		frappe.db.set_value("Reservation", res, "hold_expires_on", add_to_date(now_datetime(), minutes=-1))
		expire_holds()
		self.assertEqual(frappe.db.get_value("Reservation", res, ["status", "cancellation_note"]),
		                 ("Cancelled", HOLD_EXPIRY_NOTE))
		return res, folio

	def test_a_late_payment_is_posted_flagged_for_finance_and_never_reactivates_the_booking(self):
		self.outbox()
		res, folio = self.expired()
		body = event("payment.approved", folio)
		self.assertEqual(self.receive(body)["status"], "Needs attention")
		(row,) = self.rows(folio)
		self.assertEqual((row.reference, row.amount), (PAY_1, 1250.5))
		self.assertEqual(self.stored(body).detail, "late payment: posted")
		self.assertEqual(frappe.db.get_value("Reservation", res, "status"), "Cancelled")
		mail = frappe.get_all("Email Queue", {"reference_doctype": "UY Core Event",
		                                      "reference_name": body["event_id"]}, pluck="name")
		self.assertEqual(len(mail), 1)
		raw = frappe.db.get_value("Email Queue", mail[0], "message")  # MIME en quoted-printable
		self.assertIn(texts.ACTION_LATE_PAYMENT, quopri.decodestring(raw.encode()).decode("utf-8", "replace"))
		self.assertEqual(self.receive(body), {"duplicate": True})
		again = event("payment.approved", folio)  # otro aviso del mismo pago
		self.assertEqual(self.receive(again)["status"], "Duplicate")
		self.assertEqual(len(self.rows(folio)), 1)
		self.assertEqual(self.mails(), 1)

	def test_a_late_payment_on_a_closed_folio_is_flagged_and_not_posted(self):
		self.outbox()
		res, folio = self.expired()
		frappe.db.set_value("Folio", folio, "status", "Closed")
		body = event("payment.approved", folio)
		self.assertEqual(self.receive(body)["status"], "Needs attention")
		self.assertEqual(self.rows(folio), [])
		self.assertEqual(self.stored(body).detail, "late payment: folio closed, not posted")
		self.assertEqual(self.mails(), 1)
		self.assertEqual(frappe.db.get_value("Reservation", res, "status"), "Cancelled")

	def test_a_payment_on_a_no_show_is_a_late_payment_too(self):
		res, folio = self.booking("Confirmed")
		frappe.db.set_value("Reservation", res, "status", "No Show")
		body = event("payment.approved", folio)
		self.assertEqual(self.receive(body)["status"], "Needs attention")
		self.assertEqual(self.stored(body).detail, "late payment: posted")
		self.assertEqual(frappe.db.get_value("Reservation", res, "status"), "No Show")

	def test_a_late_payment_arrives_as_the_service_sends_it_without_a_session(self):
		# en hr-gate el aviso entra sin sesión (Guest), no como Administrator
		res, folio = self.expired()
		body = event("payment.approved", folio)
		raw, headers = signed(body)
		frappe.set_user("Guest")  # nosemgrep: frappe-setuser -- test fixture: the receiver is called without a session
		try:
			out = webhook.receive(raw, headers)
		finally:
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		self.assertEqual(out["status"], "Needs attention")
		self.assertEqual(self.stored(body).detail, "late payment: posted")
		self.assertEqual(frappe.db.get_value("Reservation", res, "status"), "Cancelled")

	def test_an_event_for_finance_is_kept_even_when_no_mail_can_go_out(self):
		# un sitio con alguien de Finanzas y sin cuenta de correo saliente (hr-gate): frappe.sendmail LANZA
		# OutgoingEmailError (HTTP 501), y el aviso se perdía con la transacción mientras core lo reintentaba (HR-224).
		# El aviso se guarda igual, sin correo, y el correo que no salió queda en el Error Log del aviso.
		for name in frappe.get_all("Email Account", {"default_outgoing": 1}, pluck="name"):
			frappe.db.set_value("Email Account", name, "default_outgoing", 0)
		for name in frappe.get_all("Email Account", {"enable_outgoing": 1}, pluck="name"):
			frappe.db.set_value("Email Account", name, "enable_outgoing", 0)
		ensure_user(FINANCE, ["Finance"])
		_, folio = self.expired()
		body = event("payment.approved", folio)
		with patch.dict(frappe.flags, {"mute_emails": False}):  # con correos silenciados, Frappe pone una cuenta falsa
			out = self.receive(body)
		self.assertEqual(out["status"], "Needs attention")
		self.assertEqual(self.stored(body).detail, "late payment: posted")
		self.assertEqual(len(self.rows(folio)), 1)
		self.assertEqual(self.mails(), 0)
		self.assertTrue(frappe.db.exists("Error Log", {"reference_doctype": "UY Core Event",
		                                               "reference_name": body["event_id"]}))

	def test_the_late_payment_action_says_what_to_do(self):
		self.assertIn("reactívala a mano", texts.ACTION_LATE_PAYMENT)
		self.assertIn("devuelve el pago desde el folio", texts.ACTION_LATE_PAYMENT)

	def test_a_refund_on_a_closed_folio_needs_attention_and_mails_finance(self):
		self.outbox()
		_, folio = self.booking()
		self.receive(event("payment.approved", folio))
		frappe.db.set_value("Folio", folio, "status", "Closed")
		body = event("payment.refunded", folio, amount="100.00")
		self.assertEqual(self.receive(body)["status"], "Needs attention")
		self.assertEqual(len(self.rows(folio)), 1)
		self.assertEqual(self.stored(body).detail, "folio_closed")
		mail = frappe.get_all("Email Queue", {"reference_doctype": "UY Core Event",
		                                      "reference_name": body["event_id"]}, pluck="name")
		self.assertEqual(len(mail), 1)
		recipients = frappe.get_all("Email Queue Recipient", {"parent": mail[0]}, pluck="recipient")
		self.assertIn(FINANCE, recipients)
		message = frappe.db.get_value("Email Queue", mail[0], "message")
		self.assertIn(texts.REVIEW_SUBJECT.format(property=PROP), message)

	def test_a_payment_on_a_closed_folio_needs_attention(self):
		self.outbox()
		_, folio = self.booking()
		frappe.db.set_value("Folio", folio, "status", "Closed")
		body = event("payment.approved", folio)
		self.assertEqual(self.receive(body)["status"], "Needs attention")
		self.assertEqual(self.rows(folio), [])
		self.assertEqual(self.mails(), 1)

	def test_another_currency_needs_attention(self):
		self.outbox()
		_, folio = self.booking()
		body = event("payment.approved", folio, currency="UYU")
		self.assertEqual(self.receive(body)["status"], "Needs attention")
		self.assertEqual(self.rows(folio), [])
		self.assertEqual(self.mails(), 1)

	def test_a_folio_that_does_not_exist_needs_attention(self):
		self.outbox()
		body = event("payment.approved", "FOLIO-NO-EXISTE")
		self.assertEqual(self.receive(body)["status"], "Needs attention")
		stored = self.stored(body)
		self.assertFalse(stored.folio)
		self.assertEqual(stored.property, PROP)
		self.assertEqual(self.mails(), 1)

	def test_an_unexpected_error_stores_nothing(self):
		_, folio = self.booking()
		body = event("payment.approved", folio)
		with patch("kamra.payments.handle_webhook", side_effect=RuntimeError("boom")), \
				self.assertRaises(RuntimeError):
			self.receive(body)
		self.assertFalse(frappe.db.exists("UY Core Event", body["event_id"]))

	def test_the_event_log_is_read_only_for_people(self):
		meta = frappe.get_meta("UY Core Event")
		roles = {p.role: p for p in meta.permissions}
		self.assertTrue({"Finance", "Hotel Admin"} <= set(roles))
		for role in ("Finance", "Hotel Admin"):
			self.assertTrue(roles[role].read)
			self.assertFalse(roles[role].write or roles[role].create or roles[role].delete)
		self.assertEqual(meta.autoname, "field:event_id")
		self.assertEqual(meta.module, "Kamra UY")


# --- alcance estricto por propiedad (HR-152) ------------------------------------------------------------------------

EVENT = "UY Core Event"
SCOPE_QUERY = "kamra.authz.property_query_conditions"
SCOPE_CHECK = "kamra.authz.property_has_permission"
READ_PATHS = ("frappe.get_list", "/api/resource (lista)", "/api/resource (ficha)", "frappe.has_permission")


def seen(doctype: str, names: list[str]) -> dict[str, set[str]]:
	"""Lo que la sesión actual alcanza de `names` por cada camino de lectura de Frappe: frappe.get_list, la lista y la
	ficha de /api/resource (frappe.api.v1, lo que sirve esa ruta) y frappe.has_permission (Desk y los enlaces)."""
	from frappe.api import v1

	listed = set(frappe.get_list(doctype, filters={"name": ["in", names]}, pluck="name", limit_page_length=0))
	rest = {"fields": json.dumps(["name"]), "filters": json.dumps({"name": ["in", names]}), "limit_page_length": 0}
	with patch.dict(frappe.form_dict, rest, clear=True):
		rest_listed = {row["name"] for row in v1.document_list(doctype)}
	rest_read = set()
	with patch.dict(frappe.form_dict, {}, clear=True):
		for name in names:
			try:
				v1.read_doc(doctype, name)
			except frappe.PermissionError:
				continue
			rest_read.add(name)
	allowed = {name for name in names if frappe.has_permission(doctype, "read", name)}
	return dict(zip(READ_PATHS, (listed, rest_listed, rest_read, allowed), strict=True))


def everywhere(names) -> dict[str, set[str]]:
	return {path: set(names) for path in READ_PATHS}


class TestCoreEventPropertyScope(CobrosCase):
	"""El registro de avisos de core guarda el folio, el pago, el estado y el detalle de cada cobro, devolución y
	contracargo: sigue el alcance por propiedad de Kamra. Con kamra_strict_property_scope, alguien de Finanzas sin User
	Permission de Property no lista ni lee ningún aviso; limitado a una propiedad no ve los de otra, con la bandera a
	False y a True. El receptor (sin sesión y con ignore_permissions) guarda igual."""

	def setUp(self):
		super().setUp()
		ensure_user(FINANCE, ["Finance"])
		frappe.db.delete("User Permission", {"user": FINANCE, "allow": "Property"})
		self.events = {PROP: self.logged(PROP), OTHER: self.logged(OTHER)}
		self.names = sorted(self.events.values())

	def tearDown(self):
		super().tearDown()
		frappe.clear_cache(user=FINANCE)  # la caché de User Permission vive fuera de la transacción deshecha

	@staticmethod
	def logged(prop: str | None) -> str:
		"""Un aviso ya guardado de `prop` (la regla de alcance solo lee la propiedad)."""
		return frappe.get_doc({"doctype": EVENT, "event_id": str(uuid.uuid4()), "event_type": "payment.approved",
		                       "status": "Applied", "property": prop, "core_property": CODE, "payment_id": PAY_1,
		                       "received_at": now_datetime()}).insert(ignore_permissions=True).name

	def as_finance(self, *properties: str):
		for p in properties:
			frappe.get_doc({"doctype": "User Permission", "user": FINANCE, "allow": "Property",
			                "for_value": p}).insert(ignore_permissions=True)
		frappe.clear_cache(user=FINANCE)
		frappe.set_user(FINANCE)  # nosemgrep: frappe-setuser -- the Finance persona under test

	@staticmethod
	def strict(on: bool = True):
		return patch.dict(frappe.conf, {"kamra_strict_property_scope": int(on)})

	def test_every_property_linked_doctype_of_the_module_is_under_the_property_scope(self):
		# los del módulo Kamra UY; UY CFE es del módulo Fiscal y queda fuera de HR-152 (cabo anotado en FIN-HR-152)
		linked = {
			row.parent for row in frappe.get_all(
				"DocField", filters={"fieldname": "property", "fieldtype": "Link", "options": "Property",
				                     "parenttype": "DocType"}, fields=["parent"])
			if frappe.db.get_value("DocType", row.parent, "module") == "Kamra UY"
			and not frappe.get_meta(row.parent).istable and not frappe.get_meta(row.parent).issingle
		}
		self.assertLessEqual({EVENT, "UY Contingency Settings", "UY Contingency Snapshot"}, linked)
		queries, checks = frappe.get_hooks("permission_query_conditions"), frappe.get_hooks("has_permission")
		for doctype in sorted(linked):
			with self.subTest(doctype=doctype):
				self.assertIn(SCOPE_QUERY, queries.get(doctype, []))
				self.assertIn(SCOPE_CHECK, checks.get(doctype, []))

	def test_strict_scope_hides_every_event_from_finance_without_property_permissions(self):
		self.as_finance()
		with self.strict():
			self.assertEqual(seen(EVENT, self.names), everywhere([]))

	def test_without_the_strict_scope_finance_without_property_permissions_reads_every_property(self):
		self.as_finance()
		with self.strict(False):
			self.assertEqual(seen(EVENT, self.names), everywhere(self.names))

	def test_finance_limited_to_one_property_never_sees_another_propertys_events(self):
		self.as_finance(OTHER)
		for on in (False, True):
			with self.subTest(strict=on), self.strict(on):
				self.assertEqual(seen(EVENT, self.names), everywhere([self.events[OTHER]]))

	def test_strict_scope_hides_an_event_without_property_from_finance_limited_to_one(self):
		orphan = self.logged(None)
		self.as_finance(OTHER)
		with self.strict():
			self.assertEqual(seen(EVENT, [orphan]), everywhere([]))

	def test_strict_scope_keeps_a_system_manager_chain_wide(self):
		ensure_user(FINANCE, ["Finance", "System Manager"])
		self.as_finance()
		with self.strict():
			self.assertEqual(seen(EVENT, self.names), everywhere(self.names))

	def test_the_receiver_still_writes_under_strict_scope_without_a_session(self):
		_, folio = self.booking()
		body = event("payment.approved", folio)
		raw, headers = signed(body)
		with self.strict():
			frappe.set_user("Guest")  # nosemgrep: frappe-setuser -- test fixture: the receiver is called without a session
			try:
				out = webhook.receive(raw, headers)
				again = webhook.receive(raw, headers)
			finally:
				frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		self.assertEqual((out["status"], again), ("Applied", {"duplicate": True}))
		stored = frappe.get_doc(EVENT, body["event_id"])
		self.assertEqual((stored.status, stored.property, stored.folio, stored.payment_id), ("Applied", PROP, folio, PAY_1))
		self.assertEqual([row.reference for row in self.rows(folio)], [PAY_1])
