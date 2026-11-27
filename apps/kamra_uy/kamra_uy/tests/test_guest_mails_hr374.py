"""HR-374: cuándo sale (y cuándo no) un correo al huésped, y desde dónde.

- la confirmación de una reserva que carga recepción (Manual, Phone, Walk-in) al pasar a Confirmed, una vez;
- el aviso de cancelación de una reserva confirmada (de la web o de recepción), una vez;
- nunca a una de plataforma ni sin correo, y nada mientras sus textos sean borradores (texts.BORRADORES_OK);
- los dos y la confirmación de la web, desde el correo de la Property («nombre» <correo>), por su propia cuenta saliente.

Lo corre `bench run-tests --app kamra_uy` (o `--module kamra_uy.tests.test_guest_mails_hr374`).
"""

import email
from email import policy
from email.utils import parseaddr
from unittest.mock import patch

import frappe
from kamra.api import _do_cancel
from kamra.tests.audit_fixtures import d, make_guest, make_property, make_reservation

from kamra_uy.notifications import cancelada, confirmation, texts
from kamra_uy.payments.tests.fixtures import PROP, CobrosCase

GUEST_MAIL = "huesped-hr374-ut@example.com"
HOTEL_MAIL = "reservas-hr374-ut@example.com"


class TestGuestMails(CobrosCase):
	def setUp(self):
		super().setUp()
		make_property(PROP, country="Uruguay", currency="USD", booking_language="", email=HOTEL_MAIL,
		              address_line="Calle UT 374, Ciudad UT", city="Ciudad UT", phone="+000 0000 0374")
		self.outbox()
		# la cuenta de los correos al huésped, como «Hotel Roosevelt Reservas» de producción (deploy/prod/lib/cuentas.py)
		frappe.get_doc({"doctype": "Email Account", "email_account_name": "UT Reservas 374", "email_id": HOTEL_MAIL,
		                "enable_outgoing": 1, "default_outgoing": 0, "smtp_server": "smtp.example.com",
		                "no_smtp_authentication": 1, "always_use_account_email_id_as_sender": 1,
		                "always_use_account_name_as_sender_name": 0}).insert(ignore_permissions=True)
		on = patch.object(texts, "BORRADORES_OK", True)
		on.start()
		self.addCleanup(on.stop)

	def desk(self, status="Confirmed", mail=GUEST_MAIL, **fields):
		guest = make_guest("UT Huésped 374", email=mail) if mail else make_guest("UT Sin correo 374")
		return make_reservation(PROP, self.rt, d(40), d(43), status=status, guest=guest,
		                        **{"source": "Manual", **fields})

	def mails(self, reservation: str) -> list[dict]:
		out = []
		for name in frappe.get_all("Email Queue", {"reference_doctype": "Reservation", "reference_name": reservation},
		                           pluck="name", order_by="creation asc"):
			doc = frappe.get_doc("Email Queue", name)
			msg = email.message_from_string(doc.message, policy=policy.default)
			out.append({"to": [r.recipient for r in doc.recipients], "subject": str(msg["Subject"]),
			            "from": parseaddr(str(msg["From"])), "reply_to": str(msg["Reply-To"] or "")})
		return out

	def logs(self, reservation: str) -> list:
		return frappe.get_all("Error Log", {"reference_name": reservation}, ["method", "error"])

	def test_a_desk_booking_gets_one_confirmation_from_the_property_mail(self):
		res = self.desk()
		sent = self.mails(res.name)
		self.assertEqual(len(sent), 1, self.logs(res.name))
		self.assertEqual(sent[0]["to"], [GUEST_MAIL])
		self.assertEqual(sent[0]["from"], (PROP, HOTEL_MAIL))
		self.assertIn(HOTEL_MAIL, sent[0]["reply_to"])
		self.assertIn("confirmada", sent[0]["subject"])
		res.reload()
		res.special_requests = "otro guardado"
		res.save(ignore_permissions=True)
		self.assertEqual(len(self.mails(res.name)), 1)

	def test_a_desk_booking_confirmed_later_gets_it_then(self):
		res = self.desk(status="Held")
		self.assertEqual(self.mails(res.name), [])
		res.reload()
		res.status = "Confirmed"
		res.save(ignore_permissions=True)
		self.assertEqual(len(self.mails(res.name)), 1)

	def test_no_confirmation_without_mail_for_a_platform_or_while_the_texts_are_drafts(self):
		self.assertEqual(self.mails(self.desk(mail="").name), [])
		self.assertEqual(self.mails(self.desk(source="OTA", channel="Booking.com").name), [])
		self.assertEqual(self.mails(self.desk(channel="Booking.com").name), [])
		self.assertEqual(self.mails(self.desk(source="AI Agent").name), [])
		with patch.object(texts, "BORRADORES_OK", False):
			self.assertEqual(self.mails(self.desk().name), [])

	def test_cancelling_a_confirmed_booking_tells_the_guest_once(self):
		res = self.desk()
		_do_cancel(frappe.get_doc("Reservation", res.name), no_fee=True)
		sent = self.mails(res.name)
		self.assertEqual(len(sent), 2, self.logs(res.name))  # la confirmación y el aviso
		self.assertIn("cancelada", sent[1]["subject"])
		self.assertEqual(sent[1]["from"], (PROP, HOTEL_MAIL))
		doc = frappe.get_doc("Reservation", res.name)
		self.assertTrue(doc.get(cancelada.SENT_FIELD))
		doc.cancellation_note = "otro guardado"
		doc.save(ignore_permissions=True)
		self.assertEqual(len(self.mails(res.name)), 2)

	def test_no_cancellation_notice_for_an_unconfirmed_a_platform_or_a_mailless_booking(self):
		for res in (self.desk(status="Held"), self.desk(source="OTA", channel="Expedia"), self.desk(mail="")):
			before = len(self.mails(res.name))
			_do_cancel(frappe.get_doc("Reservation", res.name), no_fee=True)
			self.assertEqual(len(self.mails(res.name)), before, res.name)
		with patch.object(texts, "BORRADORES_OK", False):
			res = self.desk()
		_do_cancel(frappe.get_doc("Reservation", res.name), no_fee=True)
		self.assertEqual(len(self.mails(res.name)), 1, "con borradores apagados, sin confirmación; el aviso sí")

	def test_the_planilla_load_never_mails_the_guest(self):
		frappe.flags.kamra_uy_sin_correo_al_huesped = True
		try:
			res = self.desk()
		finally:
			frappe.flags.kamra_uy_sin_correo_al_huesped = False
		self.assertEqual(self.mails(res.name), [])
		self.assertFalse(confirmation._quiet())
