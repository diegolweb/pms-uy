"""El formulario de contacto de la web (HR-314): la consulta llega por correo a recepción.

La página /contacto (y su pt y en) llama sin sesión, por POST, a `kamra_uy.contact.form.send`. El servidor valida todo
(kamra_uy.pure.contact), frena el spam sin terceros (el campo trampa, 3 s desde la carga y el contador de Frappe por IP)
y encola UN correo al destinatario del ajuste de la Property (`uy_contact_email`, por defecto
recepcion@hotelrooseveltpunta.com) con el Reply-To del huésped. El cuerpo se borra en cuanto sale (Frappe,
`redact_message_after_send`) y la fila entera, a la hora siguiente (`purge`); la que no sale, a los 7 días.

Las peticiones son de werkzeug de verdad, con su IP (TEST-NET-2), como las manda la web: sin ella, el contador de
Frappe rechaza todas. Ninguna prueba sale a la red: la cuenta saliente vive solo dentro del test y Frappe no conecta.

Lo corre `bench run-tests --app kamra_uy` (o `--module kamra_uy.contact.tests.test_contact_form`).
"""

import email
import uuid
from email import policy
from unittest.mock import patch

import frappe
from frappe.utils import add_days, now_datetime
from kamra.tests.audit_fixtures import AuditTestCase, make_property

from kamra_uy.contact import form as contact
from kamra_uy.pure import contact as rules

PROP = "UT Contacto Web"
CMD = "kamra_uy.contact.form.send"
GUEST = "huesped-contacto-ut@example.com"
DESK = "recepcion-contacto-ut@example.com"
GOOD = {"nombre": "UT Ana", "apellido": "Contacto", "email": GUEST, "telefono": "+598 99 000 314",
        "mensaje": "¿Tienen cochera?\nLlegamos el 3 de enero.", "sitio": "", "espera": "8000", "_lang": "pt"}


class TestContactForm(AuditTestCase):
	properties = (PROP,)

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		contact.ensure_fields()

	def setUp(self):
		super().setUp()
		make_property(PROP, booking_engine_enabled=1)
		frappe.db.set_value("Property", PROP, contact.FIELD, DESK)
		self.default = patch.object(contact, "default_property", return_value=PROP)
		self.default.start()
		self.addCleanup(self.default.stop)
		form, lang = frappe.local.form_dict, getattr(frappe.local, "lang", None)
		self.addCleanup(setattr, frappe.local, "form_dict", form)
		self.addCleanup(setattr, frappe.local, "lang", lang)
		self.addCleanup(frappe.set_user, "Administrator")
		self.ip = f"198.51.100.{uuid.uuid4().int % 254 + 1}"
		self.started = now_datetime()
		self.earlier_logs = set(frappe.get_all("Error Log", pluck="name"))
		self.outbox()

	def tearDown(self):
		frappe.local.outgoing_email_account = {}
		super().tearDown()

	# --- ayudas ---

	def outbox(self):
		"""Una cuenta saliente solo dentro del test (hr-gate no tiene), para que frappe.sendmail encole."""
		frappe.local.outgoing_email_account = {}
		for name in frappe.get_all("Email Account", {"default_outgoing": 1}, pluck="name"):
			frappe.db.set_value("Email Account", name, "default_outgoing", 0)
		frappe.get_doc({"doctype": "Email Account", "email_account_name": "UT Contacto", "email_id": DESK,
		                "enable_outgoing": 1, "default_outgoing": 1, "smtp_server": "smtp.example.com",
		                "no_smtp_authentication": 1}).insert(ignore_permissions=True)

	def limit_key(self) -> bytes:
		key = frappe.cache.make_key(f"rl:{CMD}:{self.ip}") + b":3600"
		self.addCleanup(frappe.cache.delete, key)
		return key

	def dispatch(self, **form):
		"""Una petición POST de la web a /api/method/<CMD>, sin sesión y desde self.ip."""
		from frappe.handler import execute_cmd
		from frappe.utils import get_url
		from werkzeug.test import EnvironBuilder

		request = EnvironBuilder(method="POST", base_url=get_url(), path=f"/api/method/{CMD}",
		                         environ_base={"REMOTE_ADDR": self.ip}).get_request()
		frappe.set_user("Guest")  # nosemgrep: frappe-setuser -- la web llama sin sesión; la limpieza vuelve a Administrator
		frappe.local.form_dict = frappe._dict({"cmd": CMD, **form})
		self.limit_key()
		try:
			with (patch.object(frappe.local, "request", request, create=True),
			      patch.object(frappe.local, "request_ip", request.remote_addr, create=True)):
				return execute_cmd(CMD)
		finally:
			frappe.set_user("Administrator")

	def send(self, **over):
		return self.dispatch(**{**GOOD, **over})

	def queued(self) -> list:
		return frappe.get_all("Email Queue", {"message_id": ["like", f"{contact.MESSAGE_ID_PREFIX}%"],
		                                      "creation": [">=", self.started]}, pluck="name", order_by="creation asc")

	def mails(self) -> list[dict]:
		out = []
		for name in self.queued():
			doc = frappe.get_doc("Email Queue", name)
			msg = email.message_from_string(doc.message, policy=policy.default)
			html = next(p.get_content() for p in msg.walk() if p.get_content_type() == "text/html")
			out.append({"doc": doc, "to": [r.recipient for r in doc.recipients], "subject": str(msg["Subject"]),
			            "reply_to": str(msg["Reply-To"] or ""), "html": html})
		return out

	def error_logs(self) -> list:
		rows = frappe.get_all("Error Log", filters={"creation": [">=", self.started]},
		                      fields=["name", "method", "error", "reference_doctype"])
		return [r for r in rows if r.name not in self.earlier_logs]

	# --- el ajuste ---

	def test_the_recipient_is_a_property_setting_with_reception_by_default(self):
		self.assertEqual(contact.DEFAULT_RECIPIENT, "recepcion@hotelrooseveltpunta.com")
		contact.ensure_fields()
		contact.ensure_fields()
		field = frappe.get_meta("Property").get_field(contact.FIELD)
		self.assertEqual((field.fieldtype, field.options, field.default), ("Data", "Email", contact.DEFAULT_RECIPIENT))
		self.assertEqual(frappe.db.get_value("Custom Field", {"dt": "Property", "fieldname": contact.FIELD}, "module"),
		                 "Kamra UY")
		self.assertEqual(frappe.db.count("Custom Field", {"dt": "Property", "fieldname": contact.FIELD}), 1)
		self.assertEqual(contact.recipient(PROP), DESK)
		frappe.db.set_value("Property", PROP, contact.FIELD, "")
		self.assertEqual(contact.recipient(PROP), contact.DEFAULT_RECIPIENT)
		frappe.db.set_value("Property", PROP, contact.FIELD, "  otra-ut@example.com ")
		self.assertEqual(contact.recipient(PROP), "otra-ut@example.com")

	def test_a_new_property_is_born_with_reception(self):
		doc = frappe.new_doc("Property")
		self.assertEqual(doc.get(contact.FIELD), contact.DEFAULT_RECIPIENT)

	# --- el correo ---

	def test_a_good_message_queues_one_mail_to_the_setting_with_the_reply_to_of_the_guest(self):
		self.assertEqual(self.send(), {"ok": True})
		(mail,) = self.mails()
		self.assertEqual(mail["to"], [DESK])
		self.assertEqual(mail["reply_to"], GUEST)
		self.assertEqual(mail["subject"], "Consulta de la web: UT Ana Contacto")
		html = mail["html"]
		for literal in ("UT Ana", "Contacto", GUEST, "+598 99 000 314", "Português", "¿Tienen cochera?",
		                "Llegamos el 3 de enero."):
			with self.subTest(literal=literal):
				self.assertIn(literal, html)
		self.assertIn("¿Tienen cochera?<br>Llegamos el 3 de enero.", html)
		doc = mail["doc"]
		self.assertEqual(doc.redact_message_after_send, 1)
		self.assertFalse(doc.reference_doctype)
		self.assertEqual(doc.status, "Not Sent")

	def test_the_mail_follows_the_setting(self):
		frappe.db.set_value("Property", PROP, contact.FIELD, "otra-ut@example.com")
		self.send()
		self.assertEqual(self.mails()[0]["to"], ["otra-ut@example.com"])

	def test_the_language_of_the_page_and_no_phone(self):
		self.send(_lang="en", telefono="")
		html = self.mails()[0]["html"]
		self.assertIn("English", html)
		self.assertNotIn("Teléfono", html)

	def test_what_the_guest_writes_is_escaped_and_cannot_add_a_header(self):
		# llega TAL CUAL (xss_safe): sin él, Frappe pasa lo de un invitado por sanitize_html, que borra el <script> entero
		# (el mensaje quedaba vacío: «requerido») y deja pasar <b>. El escape es de compose(), en el correo.
		self.assertEqual(self.send(nombre="UT <b>Ana</b>\r\nBcc: otro@example.com", mensaje="<script>alert(1)</script>"),
		                 {"ok": True})
		(mail,) = self.mails()
		self.assertNotIn("\n", mail["subject"])
		self.assertNotIn("<script>", mail["html"])
		self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", mail["html"])
		self.assertNotIn("<b>Ana</b>", mail["html"])
		self.assertIn("UT &lt;b&gt;Ana&lt;/b&gt; Bcc: otro@example.com", mail["html"])
		self.assertEqual(mail["to"], [DESK])

	# --- lo que se rechaza ---

	def test_a_bad_form_says_each_field_and_sends_nothing(self):
		out = self.send(nombre="", email="no-es-un-email", telefono="099 123 456")
		self.assertEqual(out, {"ok": False, "code": "invalido",
		                       "errors": {"nombre": "requerido", "email": "email", "telefono": "telefono"}})
		self.assertEqual(self.mails(), [])

	def test_the_trap_field_answers_ok_and_sends_nothing(self):
		self.assertEqual(self.send(sitio="https://spam.example.com"), {"ok": True})
		self.assertEqual(self.mails(), [])

	def test_too_fast_is_refused_and_sends_nothing(self):
		self.assertEqual(self.send(espera="1200"), {"ok": False, "code": "rapido"})
		self.assertEqual(self.send(espera=""), {"ok": False, "code": "rapido"})
		self.assertEqual(self.mails(), [])

	def test_the_ip_counter_of_kamra_stops_the_eleventh_in_an_hour(self):
		key = self.limit_key()
		self.assertEqual(int(frappe.cache.get(key) or 0), 0)
		self.send()
		self.assertEqual(int(frappe.cache.get(key)), 1)
		frappe.cache.setex(key, 3600, contact.LIMIT)
		with self.assertRaises(frappe.RateLimitExceededError):
			self.send()
		self.assertEqual(len(self.mails()), 1)

	def test_only_post_and_without_a_session(self):
		self.assertIn(contact.send, frappe.guest_methods)
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[contact.send], ["POST"])
		self.assertIn(contact.send, frappe.xss_safe_methods)

	def test_a_mail_that_cannot_go_says_so_and_logs_nothing_of_the_guest(self):
		for name in frappe.get_all("Email Account", {"enable_outgoing": 1}, pluck="name"):
			frappe.db.set_value("Email Account", name, {"enable_outgoing": 0, "default_outgoing": 0})
		frappe.local.outgoing_email_account = {}
		self.assertEqual(self.send(), {"ok": False, "code": "sin-envio"})
		self.assertEqual(self.mails(), [])
		logs = [r for r in self.error_logs() if r.method == contact.LOG_TITLE]
		self.assertEqual(len(logs), 1, self.error_logs())
		for secret in (GUEST, "UT Ana", "cochera", "+598 99 000 314"):
			self.assertNotIn(secret, logs[0].error or "")

	# --- cuánto se guarda ---

	def queue_row(self, status: str, days_ago: int = 0, prefix: str = contact.MESSAGE_ID_PREFIX) -> str:
		message_id = f"{prefix}{uuid.uuid4().hex}@example.com"
		frappe.sendmail(recipients=[DESK], subject="UT cola", message="UT cuerpo", delayed=True, message_id=message_id)
		name = frappe.db.get_value("Email Queue", {"message_id": message_id})
		frappe.db.set_value("Email Queue", name, {"status": status, "creation": add_days(now_datetime(), -days_ago)},
		                    update_modified=False)
		return name

	def test_purge_deletes_the_sent_ones_and_the_unsent_after_seven_days_and_nothing_else(self):
		self.assertEqual(contact.UNSENT_DAYS, 7)
		sent = self.queue_row("Sent")
		new = self.queue_row("Not Sent", days_ago=1)
		old = self.queue_row("Error", days_ago=8)
		other = self.queue_row("Sent", prefix="ut-otro.")
		contact.purge()
		self.assertFalse(frappe.db.exists("Email Queue", sent))
		self.assertFalse(frappe.db.exists("Email Queue", old))
		self.assertFalse(frappe.db.exists("Email Queue Recipient", {"parent": ["in", [sent, old]]}))
		self.assertTrue(frappe.db.exists("Email Queue", new))
		self.assertTrue(frappe.db.exists("Email Queue", other))

	# --- el cableado ---

	def test_the_hooks_install_the_setting_and_purge_every_hour(self):
		hooks = frappe.get_hooks(app_name="kamra_uy")
		for when in ("after_install", "after_migrate"):
			self.assertIn("kamra_uy.contact.form.ensure_fields", hooks[when])
		self.assertIn("kamra_uy.contact.form.purge", frappe.get_hooks("scheduler_events")["hourly"])

	def test_the_rules_are_the_pure_ones(self):
		self.assertIs(contact.check, rules.check)
		self.assertEqual(contact.LIMIT, 10)
