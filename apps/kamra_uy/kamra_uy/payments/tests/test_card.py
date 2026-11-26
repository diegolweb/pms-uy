"""La tarjeta y el botón del saldo de una reserva web retenida, por su token de estado (HR-222; KICKOFF-HR-220 §7).

`kamra_uy.payments.card.checkout`, `pay` y `wallet` son públicas (sin sesión, solo POST): validan el token de estado,
exigen la retención viva, aseguran el enlace del folio por lo que falta pagar y piden a core (HR-221) el cobro con el
token de la tarjeta o la preferencia del saldo. El importe nunca lo pone el navegador. Devuelven códigos, no textos.
Core es un doble: ninguna prueba sale a la red.

Lo corre `bench run-tests --app kamra_uy` (o `--module kamra_uy.payments.tests.test_card`).
"""

import hashlib
import inspect
from datetime import datetime, timedelta
from unittest.mock import patch

import frappe
from frappe.utils import now_datetime

from kamra import public_api
from kamra.tests.audit_fixtures import d, make_property, make_room
from kamra_uy.core.client import CoreError
from kamra_uy.payments import card
from kamra_uy.payments.tests.fixtures import PROP, CobrosCase

CORE = "http://core.example.com"
ORIGIN = "https://hotel.example.com"
PUBLIC_KEY = "TEST-00000000-not-a-real-public-key"
CHARGE = {"amount": "4478.14", "currency": "UYU", "exchange_rate": "40.7104",
          "rate_as_of": "2026-11-25T00:00:00.000-04:00"}
# HR-368: las equivalencias informativas de core (USD 110 al cambio del simulador), con una que la web no pinta
EQUIVALENTS = [{"currency": "USD", "amount": "110.00"}, {"currency": "BRL", "amount": "647.08"},
               {"currency": "EUR", "amount": "100.00"}, {"currency": "ARS", "amount": "176440.00"}]
WALLET_URL = "http://mp-mock:8080/checkout/v1/redirect?pref_id=wallet-1"
SENTINEL_TOKEN = "SENTINEL-TOK"
SENTINEL_DOC = "99999999"
APPROVED = {"payment_id": "0b0c1d2e-1111-4222-8333-777777777777", "provider_payment_id": "1234567890",
            "status": "approved", "status_detail": "accredited", "three_ds": None}


class Core:
	"""El doble de CoreClient: enlaces, su `card`, el cobro con tarjeta y el saldo. Apunta cada llamada.

	Como core (`services/core/src/idempotency.ts`), la misma Idempotency-Key con otra ruta u otro cuerpo da 409
	`idempotency_conflict`; y el saldo, como `createWalletPreference`, es una preferencia por enlace."""

	def __init__(self, public_key: str | None = PUBLIC_KEY, card_reply: dict | None = None):
		self.public_key = public_key
		# HR-366: lo que cobra la tarjeta, en pesos (`card.charge` de core); None, Mercado Pago no dio el cambio
		self.charge: dict | None = dict(CHARGE)
		self.card_reply = card_reply or APPROVED
		self.fail: dict[str, Exception] = {}
		self.calls: list[tuple] = []
		self.links = 0
		self.keys: dict[str, tuple] = {}

	def __call__(self, base_url, api_key, *a, **k):
		return self

	def post(self, path, body, idempotency_key):
		self.calls.append(("POST", path, body, idempotency_key))
		kind = path.rsplit("/", 1)[-1]
		if kind in self.fail:
			raise self.fail[kind]
		if self.keys.setdefault(idempotency_key, (path, body)) != (path, body):
			raise CoreError(409, "idempotency_conflict", "la Idempotency-Key ya se usó con otra petición")
		if path == "/v1/payments/links":
			self.links += 1
			return {"link_id": f"link-{self.links}", "url": f"http://mp-mock:8080/checkout/v1/redirect?pref_id={self.links}"}
		if kind == "card-payments":
			return self.card_reply
		if kind == "wallet":
			return {"url": WALLET_URL}
		raise AssertionError(f"ruta inesperada {path}")

	def get(self, path):
		self.calls.append(("GET", path, None, None))
		if "get" in self.fail:
			raise self.fail["get"]
		return {"link_id": path.rsplit("/", 1)[-1], "url": "http://mp/x", "status": "pending", "expires_at": None,
		        "card": {"public_key": self.public_key, "charge": self.charge, "equivalents": EQUIVALENTS}
		        if self.public_key and self.charge else None}

	def after(self, n: int) -> list[tuple]:
		return self.calls[n:]


class TestCard(CobrosCase):
	def setUp(self):
		super().setUp()
		frappe.local.lang = "en"
		make_property(PROP, country="Uruguay", currency="USD", uy_iva_room_pct=10, uy_iva_standard_pct=22,
		              booking_engine_enabled=1, booking_mode="Instant", booking_payment_mode="Full online",
		              hold_minutes=30, advance_percent=0, registration_fee=0, cleaning_fee=0)
		frappe.db.set_value("Room Type", self.rt, {"base_price": 130, "adults_capacity": 2})
		make_room(PROP, self.rt, "C101")
		conf = patch.dict(frappe.conf, {"hr_core_base_url": CORE, "booking_return_origins": [ORIGIN]})
		conf.start()
		self.addCleanup(conf.stop)
		self.core = Core()
		double = patch("kamra_uy.payments.mercadopago.CoreClient", self.core)
		double.start()
		self.addCleanup(double.stop)
		self.started = now_datetime() - timedelta(seconds=1)
		# el Error Log no se deshace con el rollback: los del test anterior (en ese segundo) no son de este
		self.earlier_logs = set(frappe.get_all("Error Log", filters={"creation": [">=", self.started]}, pluck="name"))

	def held(self) -> dict:
		"""Una reserva web retenida, hecha como la hace la web (`book`), con su enlace del folio por el total."""
		out = public_api.book(property=PROP, room_type=self.rt, check_in_date=d(20), check_out_date=d(23),
		                      guest_name="UT Huésped Tarjeta", phone="+59899000222", email="tarjeta-ut@example.com",
		                      adults=2)
		self.assertEqual(out["status"], "Pending Payment", out)
		self.assertGreater(out["advance_due"], 0)
		return out

	def folio_link(self, reservation: str) -> str:
		return frappe.db.get_value("Folio", {"reservation": reservation, "folio_type": "Guest"}, "payment_link_id")

	def pay(self, token: str, **over) -> dict:
		args = {"card_token": "tok-card-ut-1", "payment_method_id": "visa",
		        "payer_email": "pagador-ut@example.com", "charge_amount": CHARGE["amount"],
		        "charge_currency": CHARGE["currency"]}
		args.update(over)
		return card.pay(token, **args)

	def error_logs(self) -> list[str]:
		rows = frappe.get_all("Error Log", filters={"creation": [">=", self.started]}, fields=["name", "method", "error"])
		return [f"{r.method}\n{r.error}" for r in rows if r.name not in self.earlier_logs]

	# --- paso 0: lo que importa de kamra.public_api (código del hotel, HR-31) ----------------------------------

	def test_the_public_api_it_imports_keeps_its_signatures(self):
		expected = {
			"_res_by_status_token": ["token"],
			"_hold_is_live": ["res"],
			"_guest_folio": ["reservation"],
			"_deposit_link": ["reservation", "amount", "return_url"],
			"_return_url": ["return_url", "token"],
		}
		for name, params in expected.items():
			fn = getattr(public_api, name)
			self.assertIs(getattr(card, name), fn, name)
			self.assertEqual(list(inspect.signature(fn).parameters), params, name)
		out = self.held()
		folio = public_api._guest_folio(out["reservation"])
		self.assertTrue({"name", "payment_link_url", "payment_link_amount"} <= set(folio))

	def test_the_three_are_public_and_post_only(self):
		for fn in (card.checkout, card.pay, card.wallet):
			self.assertIn(fn, frappe.whitelisted)
			self.assertIn(fn, frappe.guest_methods)
			self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[fn], ["POST"])
		self.assertEqual(list(inspect.signature(card.checkout).parameters), ["token"])
		self.assertEqual(list(inspect.signature(card.pay).parameters), [
			"token", "card_token", "payment_method_id", "issuer_id", "payer_email", "identification_type",
			"identification_number", "device_id", "charge_amount", "charge_currency"])
		self.assertEqual(list(inspect.signature(card.wallet).parameters), ["token", "return_url"])

	# --- el token de estado y la retención -----------------------------------------------------------------

	def test_a_false_token_is_refused(self):
		out = self.held()
		name, _, sig = out["status_token"].rpartition(".")
		bad = f"{name}.{('0' if sig[0] != '0' else '1')}{sig[1:]}"
		n = len(self.core.calls)
		for call in (lambda t: card.checkout(t), lambda t: self.pay(t), lambda t: card.wallet(t, "")):
			for wrong in (bad, "nonsense", ""):
				with self.assertRaises(frappe.PermissionError):
					call(wrong)
		self.assertEqual(self.core.after(n), [])

	def test_an_expired_hold_is_hold_expired_and_core_is_not_asked(self):
		out = self.held()
		frappe.db.set_value("Reservation", out["reservation"], "hold_expires_on", "2000-01-01 00:00:00")
		n = len(self.core.calls)
		self.assertEqual(self.pay(out["status_token"])["code"], "hold_expired")
		self.assertEqual(card.wallet(out["status_token"], "")["code"], "hold_expired")
		got = card.checkout(out["status_token"])
		self.assertEqual((got["code"], got["card"], got["hold_expires_on"]), ("hold_expired", False, None))
		self.assertEqual(self.core.after(n), [])

	def test_a_paid_booking_is_not_payable_and_core_is_not_asked(self):
		out = self.held()
		frappe.db.set_value("Reservation", out["reservation"], "advance_paid", out["advance_due"])
		n = len(self.core.calls)
		self.assertEqual(self.pay(out["status_token"])["code"], "link_not_payable")
		self.assertEqual(card.wallet(out["status_token"], "")["code"], "link_not_payable")
		self.assertEqual(self.core.after(n), [])

	def test_a_second_press_after_the_booking_is_confirmed_is_not_payable_not_expired(self):
		# el doble clic tras el aprobado: la reserva ya no está retenida, pero no «venció»
		out = self.held()
		frappe.db.set_value("Reservation", out["reservation"], {"status": "Confirmed",
		                                                         "advance_paid": out["advance_due"]})
		n = len(self.core.calls)
		self.assertEqual(self.pay(out["status_token"])["code"], "link_not_payable")
		self.assertEqual(card.checkout(out["status_token"])["code"], "link_not_payable")
		self.assertEqual(self.core.after(n), [])

	# --- checkout ----------------------------------------------------------------------------------------

	def test_checkout_gives_the_card_key_and_the_full_amount(self):
		out = self.held()
		got = card.checkout(out["status_token"])
		self.assertEqual(set(got), {"card", "public_key", "amount", "currency", "charge", "equivalents", "hold_expires_on",
		                            "code"})
		self.assertEqual((got["card"], got["public_key"], got["currency"], got["code"]), (True, PUBLIC_KEY, "USD", None))
		# HR-366: lo que cobra la tarjeta, en pesos, para que la web lo enseñe
		self.assertEqual(got["charge"], {"amount": 4478.14, "currency": "UYU", "exchange_rate": "40.7104"})
		# HR-368: las equivalencias, en el orden de core, sin lo que la web no pinta
		self.assertEqual(got["equivalents"], [{"amount": 110.0, "currency": "USD"}, {"amount": 647.08, "currency": "BRL"},
		                                      {"amount": 176440.0, "currency": "ARS"}])
		# «Full online» (D-HR-38): lo que se paga al reservar es el total
		self.assertEqual(got["amount"], out["amount_after_tax"])
		self.assertEqual(got["amount"], out["advance_due"])
		self.assertIsNotNone(datetime.fromisoformat(got["hold_expires_on"]).tzinfo)
		self.assertEqual(self.core.calls[-1][:2], ("GET", f"/v1/payments/links/{self.folio_link(out['reservation'])}"))

	def test_checkout_without_a_public_key_has_no_card(self):
		self.core.public_key = None
		got = card.checkout(self.held()["status_token"])
		self.assertEqual((got["card"], got["public_key"], got["code"]), (False, None, None))

	def test_checkout_without_the_pesos_has_no_card(self):
		# HR-366 (D-S366-4): core no da `charge` si Mercado Pago no da el cambio; sin él no hay tarjeta en la web
		self.core.charge = None
		got = card.checkout(self.held()["status_token"])
		self.assertEqual((got["card"], got["public_key"], got["charge"], got["code"]), (False, None, None, None))

	def test_checkout_of_a_property_without_mercado_pago_has_no_card(self):
		out = self.held()
		frappe.db.set_value("Payment Gateway Settings", {"property": PROP}, "enabled", 0)
		n = len(self.core.calls)
		got = card.checkout(out["status_token"])
		self.assertEqual((got["card"], got["public_key"], got["code"]), (False, None, None))
		self.assertEqual(self.pay(out["status_token"])["code"], "card_not_enabled")
		self.assertEqual(self.core.after(n), [])

	def test_checkout_with_core_down_has_no_card_and_says_unavailable(self):
		out = self.held()
		self.core.fail["get"] = CoreError(None, "upstream_unavailable", "sin respuesta", retryable=True)
		got = card.checkout(out["status_token"])
		self.assertEqual((got["card"], got["code"]), (False, "unavailable"))

	def test_checkout_with_kamra_missing_the_core_url_says_unavailable_and_logs_it(self):
		# lo que pasa si una petición web no ve aún `hr_core_base_url` (caché de 60 s de site_config): no es «sin
		# tarjeta», que escondería la avería; es «más tarde», con su Error Log para recepción
		out = self.held()
		self.core.fail["get"] = CoreError(None, "not_configured", "Falta configurar la dirección del servicio de cobros")
		got = card.checkout(out["status_token"])
		self.assertEqual((got["card"], got["public_key"], got["code"]), (False, None, "unavailable"))
		self.assertTrue(any("not_configured" in text for text in self.error_logs()), "la avería deja su Error Log")

	# --- pay ------------------------------------------------------------------------------------------

	def test_pay_charges_the_folio_link_without_an_amount(self):
		out = self.held()
		link = self.folio_link(out["reservation"])
		n = len(self.core.calls)
		got = self.pay(out["status_token"], issuer_id="310", identification_type="CI", identification_number="12345678",
		               device_id="armor.0123456789abcdef")
		self.assertEqual(got, {"status": "approved", "status_detail": "accredited", "payment_id": APPROVED["payment_id"],
		                       "provider_payment_id": "1234567890", "three_ds": None, "code": None})
		((method, path, body, key),) = self.core.after(n)
		self.assertEqual((method, path), ("POST", f"/v1/payments/links/{link}/card-payments"))
		self.assertEqual(body, {"token": "tok-card-ut-1", "payment_method_id": "visa",
		                        "issuer_id": "310", "payer": {"email": "pagador-ut@example.com", "identification": {
		                            "type": "CI", "number": "12345678"}}, "device_id": "armor.0123456789abcdef",
		                        "expected_charge": {"amount": "4478.14", "currency": "UYU"}})
		self.assertEqual(key, hashlib.sha256(f"{link}:tok-card-ut-1".encode()).hexdigest())

	def test_the_amount_a_client_sends_never_reaches_core(self):
		out = self.held()
		self.assertNotIn("amount", inspect.signature(card.pay).parameters)
		n = len(self.core.calls)
		frappe.call(card.pay, token=out["status_token"], card_token="tok-1", payment_method_id="visa",
		            payer_email="pagador-ut@example.com", charge_amount="4478.14", charge_currency="UYU", amount=1,
		            transaction_amount=1)
		((_, _, body, _),) = self.core.after(n)
		self.assertFalse({"amount", "transaction_amount"} & set(body))

	def test_the_same_card_token_twice_gives_the_same_key(self):
		out = self.held()
		n = len(self.core.calls)
		self.pay(out["status_token"], card_token="tok-twice")
		self.pay(out["status_token"], card_token="tok-twice")
		self.pay(out["status_token"], card_token="tok-other")
		keys = [c[3] for c in self.core.after(n)]
		self.assertEqual(keys[0], keys[1])
		self.assertNotEqual(keys[0], keys[2])

	def test_without_an_email_it_pays_as_the_guest_of_the_booking(self):
		out = self.held()
		n = len(self.core.calls)
		self.pay(out["status_token"], payer_email="")
		self.assertEqual(self.core.after(n)[0][2]["payer"], {"email": "tarjeta-ut@example.com"})

	def test_a_link_for_another_amount_is_replaced_by_one_for_what_is_left(self):
		out = self.held()
		old = self.folio_link(out["reservation"])
		folio = frappe.db.get_value("Folio", {"reservation": out["reservation"], "folio_type": "Guest"})
		frappe.db.set_value("Folio", folio, "payment_link_amount", 1)
		n = len(self.core.calls)
		self.pay(out["status_token"])
		(made, charged) = self.core.after(n)
		self.assertEqual((made[1], made[2]["amount"]), ("/v1/payments/links", f"{out['advance_due']:.2f}"))
		new = self.folio_link(out["reservation"])
		self.assertNotEqual(new, old)
		self.assertEqual(charged[1], f"/v1/payments/links/{new}/card-payments")

	def test_card_data_core_would_refuse_is_invalid_and_core_is_not_asked(self):
		out = self.held()
		n = len(self.core.calls)
		self.assertEqual(self.pay(out["status_token"], card_token="")["code"], "invalid")
		self.assertEqual(self.pay(out["status_token"], payment_method_id="")["code"], "invalid")
		self.assertEqual(self.pay(out["status_token"], payer_email="no-es-un-correo")["code"], "invalid")
		self.assertEqual(self.core.after(n), [])

	def test_a_3ds_challenge_reaches_the_web(self):
		three_ds = {"external_resource_url": "https://acs.example.com/c", "creq": "eyJjcmVxIjoiMSJ9"}
		self.core.card_reply = {"payment_id": "p-3ds", "status": "pending", "status_detail": "pending_challenge",
		                        "three_ds": three_ds}
		got = self.pay(self.held()["status_token"])
		self.assertEqual((got["status"], got["three_ds"], got["code"]), ("pending", three_ds, None))

	def test_a_rejected_card_says_why_with_mercado_pago_code(self):
		self.core.card_reply = {"payment_id": "p-r", "status": "rejected", "status_detail": "cc_rejected_other_reason",
		                        "three_ds": None}
		got = self.pay(self.held()["status_token"])
		self.assertEqual((got["status"], got["status_detail"], got["code"]),
		                 ("rejected", "cc_rejected_other_reason", None))

	def test_what_core_refuses_comes_back_as_its_code(self):
		out = self.held()
		for error, code in [
			(CoreError(409, "too_many_attempts"), "too_many_attempts"),
			(CoreError(409, "link_not_payable"), "link_not_payable"),
			(CoreError(409, "card_not_enabled"), "card_not_enabled"),
			(CoreError(422, "cc_rejected_bad_filled_card_number"), "invalid"),
			(CoreError(502, "currency_mismatch", retryable=True), "unavailable"),
		]:
			self.core.fail["card-payments"] = error
			got = self.pay(out["status_token"])
			self.assertEqual((got["code"], got["payment_id"], got["three_ds"]), (code, None, None), error)

	def test_with_core_down_it_is_unavailable_and_the_error_log_has_no_card_data(self):
		out = self.held()
		self.core.fail["card-payments"] = CoreError(None, "upstream_unavailable", "POST: sin respuesta", retryable=True)
		got = self.pay(out["status_token"], card_token=SENTINEL_TOKEN, identification_type="CI",
		               identification_number=SENTINEL_DOC)
		self.assertEqual(got["code"], "unavailable")
		logs = self.error_logs()
		self.assertTrue(logs, "el fallo de core deja su Error Log")
		for text in logs:
			self.assertNotIn(SENTINEL_TOKEN, text)
			self.assertNotIn(SENTINEL_DOC, text)

	def test_an_unexpected_failure_is_unavailable_and_its_log_has_no_card_data(self):
		out = self.held()
		# un fallo cuyo mensaje lleva el token y el documento: el Error Log los tacha
		self.core.fail["card-payments"] = RuntimeError(f"boom {SENTINEL_TOKEN} {SENTINEL_DOC}")
		got = self.pay(out["status_token"], card_token=SENTINEL_TOKEN, identification_type="CI",
		               identification_number=SENTINEL_DOC)
		self.assertEqual(got["code"], "unavailable")
		logs = self.error_logs()
		self.assertTrue(logs)
		for text in logs:
			self.assertNotIn(SENTINEL_TOKEN, text)
			self.assertNotIn(SENTINEL_DOC, text)

	# --- wallet ---------------------------------------------------------------------------------------

	def test_wallet_gives_the_url_of_the_balance_preference(self):
		out = self.held()
		link = self.folio_link(out["reservation"])
		n = len(self.core.calls)
		self.assertEqual(card.wallet(out["status_token"], f"{ORIGIN}/reservar/gracias?lang=pt"),
		                 {"url": WALLET_URL, "code": None})
		self.assertEqual(card.wallet(out["status_token"], ""), {"url": WALLET_URL, "code": None})
		first, second = self.core.after(n)
		# la vuelta, validada y con el token de estado, como la de book; sin ella, la del enlace
		self.assertEqual((first[0], first[1], first[2]), ("POST", f"/v1/payments/links/{link}/wallet", {
			"return_url": f"{ORIGIN}/reservar/gracias?lang=pt&booking={out['status_token']}"}))
		self.assertEqual((second[1], second[2]), (f"/v1/payments/links/{link}/wallet", {}))
		# otro cuerpo, otra clave (HR-238): core devuelve la preferencia que ya guardó, no un 409
		self.assertNotEqual(first[3], second[3])

	def test_wallet_again_in_another_language_gives_the_same_url_not_invalid(self):
		# HR-238: el huésped pulsa «Pagar con saldo», cambia de idioma y lo vuelve a pulsar
		out = self.held()
		n = len(self.core.calls)
		for lang in ("es", "pt", "es", "en"):
			self.assertEqual(card.wallet(out["status_token"], f"{ORIGIN}/reservar/gracias?lang={lang}"),
			                 {"url": WALLET_URL, "code": None}, lang)
		keys = [call[3] for call in self.core.after(n)]
		self.assertEqual(keys[0], keys[2], "el mismo cuerpo, la misma clave")
		self.assertEqual(len(set(keys)), 3)
		self.assertEqual(self.error_logs(), [])

	def test_an_idempotency_conflict_of_core_leaves_an_error_log(self):
		out = self.held()
		self.core.fail["wallet"] = CoreError(409, "idempotency_conflict", "la Idempotency-Key ya se usó con otra petición")
		self.assertEqual(card.wallet(out["status_token"], ""), {"url": None, "code": "invalid"})
		logs = self.error_logs()
		self.assertEqual(len(logs), 1, logs)
		self.assertIn("idempotency_conflict", logs[0])

	def test_an_idempotency_conflict_of_a_card_payment_leaves_an_error_log_without_card_data(self):
		out = self.held()
		self.core.fail["card-payments"] = CoreError(409, "idempotency_conflict", f"boom {SENTINEL_TOKEN} {SENTINEL_DOC}")
		got = self.pay(out["status_token"], card_token=SENTINEL_TOKEN, identification_type="CI",
		               identification_number=SENTINEL_DOC)
		self.assertEqual(got["code"], "invalid")
		logs = self.error_logs()
		self.assertTrue(any("idempotency_conflict" in text for text in logs), logs)
		for text in logs:
			self.assertNotIn(SENTINEL_TOKEN, text)
			self.assertNotIn(SENTINEL_DOC, text)

	def test_wallet_refuses_a_return_to_another_site_before_anything(self):
		out = self.held()
		n = len(self.core.calls)
		self.assertEqual(card.wallet(out["status_token"], "https://evil.example/x"), {"url": None, "code": "invalid"})
		self.assertEqual(self.core.after(n), [])

	def test_a_new_link_for_the_wallet_goes_back_to_the_page_it_names(self):
		out = self.held()
		folio = frappe.db.get_value("Folio", {"reservation": out["reservation"], "folio_type": "Guest"})
		frappe.db.set_value("Folio", folio, "payment_link_amount", 1)
		n = len(self.core.calls)
		card.wallet(out["status_token"], f"{ORIGIN}/reservar/gracias")
		made = self.core.after(n)[0]
		self.assertEqual(made[1], "/v1/payments/links")
		self.assertTrue(made[2]["return_url"].startswith(f"{ORIGIN}/reservar/gracias?booking="), made[2]["return_url"])

	def test_wallet_with_core_down_is_unavailable(self):
		out = self.held()
		self.core.fail["wallet"] = CoreError(503, "upstream_unavailable", retryable=True)
		self.assertEqual(card.wallet(out["status_token"], ""), {"url": None, "code": "unavailable"})
