"""El paquete uruguayo dentro de Kamra: se resuelve por el hook, cotiza con las tasas configurables de la propiedad,
nombra el impuesto «IVA» en la cuenta, cobra con «Tarjeta local» y «Tarjeta extranjera» y toma el tipo de cambio del BCU
(con `requests.post` parcheado con la respuesta guardada: ningún test llama al BCU).

Lo corre `bench run-tests --app kamra_uy`.
"""

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from kamra import api, cashier, ledger, localization
from kamra.tests.audit_fixtures import (
	TODAY,
	AuditTestCase,
	d,
	guest_folio,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)
from kamra_uy.localization import uruguay
from kamra_uy.pure import bcu

PROP_UY = "UT UY Roosevelt"
BCU_REPLY = (Path(__file__).resolve().parents[3] / "tests" / "unit" / "uy" / "fixtures"
             / "bcu-cotizaciones-2026-11-12.xml").read_bytes()
FRIDAY = "2026-11-13"
UY_MODES = ["Cash", "Tarjeta local", "Tarjeta extranjera", "Bank Transfer"]
# los Select de Kamra donde se guarda un modo de pago: el paquete les añade los suyos (fixtures/property_setter.json)
MODE_SELECTS = (("Folio Payment", "mode"), ("Cashier Transaction", "mode"), ("POS Order", "payment_mode"),
                ("Banquet Receipt", "mode"))


def _ensure_currency(code: str) -> None:
	if not frappe.db.exists("Currency", code):
		frappe.get_doc({"doctype": "Currency", "currency_name": code, "enabled": 1}).insert(ignore_permissions=True)


def make_uy_property(**fields) -> str:
	_ensure_currency("UYU")
	payload = {"country": "Uruguay", "currency": "UYU", "business_date": TODAY,
	           "uy_iva_room_pct": 10, "uy_iva_standard_pct": 22}
	payload.update(fields)
	return make_property(PROP_UY, **payload)


class TestUruguayResolves(IntegrationTestCase):
	def test_hook_claims_uruguay(self):
		self.assertEqual(localization.pack_for_country("Uruguay").__name__, "kamra_uy.localization.uruguay")

	def test_supported_countries_lists_uruguay(self):
		countries = {row["country"]: row for row in localization.supported_countries()}
		self.assertEqual(countries["Uruguay"]["currency"], "UYU")
		self.assertEqual(countries["Uruguay"]["timezone"], "America/Montevideo")
		self.assertEqual(countries["Uruguay"]["tax_label"], "IVA")
		self.assertEqual(countries["Uruguay"]["tax_id_label"], "RUT")

	def test_custom_fields_are_installed(self):
		prop = frappe.get_meta("Property")
		self.assertEqual(prop.get_field("uy_iva_room_pct").fieldtype, "Percent")
		self.assertEqual(str(prop.get_field("uy_iva_room_pct").default), "10")
		self.assertEqual(str(prop.get_field("uy_iva_standard_pct").default), "22")
		residence = frappe.get_meta("Guest").get_field("residence_country")
		self.assertEqual((residence.fieldtype, residence.options), ("Link", "Country"))

	def test_payment_mode_selects_keep_kamra_options_and_add_the_cards(self):
		import json

		for doctype, fieldname in MODE_SELECTS:
			with self.subTest(doctype=doctype):
				path = Path(frappe.get_app_path("kamra", "kamra", "doctype", frappe.scrub(doctype),
				                                f"{frappe.scrub(doctype)}.json"))
				standard = next(f["options"] for f in json.loads(path.read_text())["fields"]
				                if f["fieldname"] == fieldname).split("\n")
				options = frappe.get_meta(doctype).get_field(fieldname).options.split("\n")
				# la lista ENTERA: si Kamra suma o quita un modo, la guarda falla hasta alinear el fixture
				self.assertEqual(options, standard + ["Tarjeta local", "Tarjeta extranjera"])


class TestUruguayPack(AuditTestCase):
	properties = (PROP_UY,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_uy_property()

	def test_pack_for_the_property(self):
		self.assertIs(localization.pack_for(PROP_UY), uruguay)

	def test_default_rates(self):
		self.assertEqual(uruguay.calculate_room_tax(PROP_UY, None, 1000), Decimal("10"))
		self.assertEqual(uruguay.fnb_tax_rate(PROP_UY), 22.0)
		self.assertEqual(localization.services_tax_rate(uruguay, PROP_UY), 22.0)
		self.assertEqual(uruguay.tax_rate_options(PROP_UY), [0, 10, 22])

	def test_room_type_rate_wins(self):
		rt = frappe.get_doc("Room Type", make_room_type(PROP_UY, "UY12", tax_percent=12))
		self.assertEqual(uruguay.calculate_room_tax(PROP_UY, rt, 1000), Decimal("12"))

	def test_rates_are_configured_per_property(self):
		make_uy_property(uy_iva_room_pct=11, uy_iva_standard_pct=20)
		self.assertEqual(uruguay.calculate_room_tax(PROP_UY, {"tax_percent": 0}, 1000), Decimal("11"))
		self.assertEqual(uruguay.fnb_tax_rate(PROP_UY), 20.0)
		self.assertEqual(uruguay.tax_rate_options(PROP_UY), [0, 11, 20])

	def test_india_rates_of_the_property_are_not_used(self):
		make_uy_property(gst_rate_low=5, gst_rate_high=18, uy_iva_room_pct=0, uy_iva_standard_pct=0)
		self.assertEqual(uruguay.calculate_room_tax(PROP_UY, None, 1000), Decimal("10"))
		self.assertEqual(uruguay.fnb_tax_rate(PROP_UY), 22.0)

	def test_mercado_pago_is_the_online_gateway(self):
		self.assertEqual(localization.payment_gateways(localization.pack_for(PROP_UY)), ["Mercado Pago"])
		from kamra_uy.payments import mercadopago

		self.assertIs(uruguay.payment_provider("Mercado Pago"), mercadopago)
		with self.assertRaises(frappe.ValidationError):
			uruguay.payment_provider("Razorpay")

	def test_front_desk_vocabulary(self):
		self.assertEqual(localization.payment_modes(uruguay), UY_MODES)
		self.assertEqual(localization.instant_payment_modes(uruguay), ["Tarjeta local", "Tarjeta extranjera"])
		localization.assert_payment_mode(PROP_UY, "Tarjeta extranjera")
		localization.assert_payment_mode(PROP_UY, "Tarjeta local")
		with self.assertRaises(frappe.ValidationError):
			localization.assert_payment_mode(PROP_UY, "UPI")
		ids = localization.id_types(uruguay)
		self.assertIn("DNI", ids)
		self.assertIn("Passport", ids)
		self.assertEqual(localization.default_currency(uruguay), "UYU")
		self.assertEqual(localization.home_country_iso3(uruguay), "URY")
		self.assertEqual(localization.privacy_terms(uruguay)["notice_languages"], ["es", "pt", "en"])

	def test_locale_symbols(self):
		loc = uruguay.locale(frappe.get_doc("Property", PROP_UY))
		self.assertEqual((loc["locale"], loc["currency"], loc["currency_symbol"]), ("es-UY", "UYU", "$"))
		self.assertEqual((loc["tax_label"], loc["tax_id_label"], loc["tax_rates"]), ("IVA", "RUT", [0, 10, 22]))
		self.assertEqual(uruguay.locale(frappe._dict(currency="USD"))["currency_symbol"], "US$")
		brl = (frappe.db.get_value("Currency", "BRL", "symbol") or "").strip() or "BRL "
		self.assertEqual(uruguay.locale(frappe._dict(currency="BRL"))["currency_symbol"], brl)
		self.assertEqual(localization.money_text(PROP_UY, 1250), "$1,250")

	def test_amount_in_words_follows_the_property_currency(self):
		prop = frappe.get_doc("Property", PROP_UY)
		self.assertEqual(localization.amount_in_words(uruguay, prop, 1250.5),
		                 "pesos uruguayos mil doscientos cincuenta con 50/100")
		self.assertTrue(uruguay.amount_in_words(frappe._dict(currency="USD"), 1).startswith("dólares"))

	def test_invoice_context(self):
		ctx = uruguay.invoice_context(frappe._dict(name=None))
		self.assertEqual({k: ctx[k] for k in ("tax_label", "tax_id_label", "service_code", "sac", "place_of_supply")},
		                 {"tax_label": "IVA", "tax_id_label": "RUT", "service_code": None, "sac": None,
		                  "place_of_supply": None})
		self.assertEqual(ctx["split"], [("iva", Decimal("1"))])
		self.assertIn("no es un comprobante fiscal", ctx["footer"])
		self.assertEqual(localization.invoice_title(uruguay), "Detalle de cuenta")

	def _stay(self):
		rt = make_room_type(PROP_UY, "STD", base_price=3000)
		res = make_reservation(PROP_UY, rt, d(-1), d(1), status="Checked In", room=make_room(PROP_UY, rt, "UY101"))
		return guest_folio(res.name)

	def test_the_bill_names_iva_and_rut_and_never_gst(self):
		bill = api.folio_invoice(self._stay())
		self.assertEqual(bill["property"]["tax_label"], "IVA")
		self.assertEqual(bill["property"]["tax_id_label"], "RUT")
		self.assertIsNone(bill["property"]["place_of_supply"])
		labels = {**bill["property"], **bill["document"]}
		for key, value in labels.items():
			if isinstance(value, str) and ("label" in key or key in ("title", "footer", "tax_note")):
				self.assertNotIn("GST", value, key)
		self.assertTrue(bill["document"]["amount_in_words"].startswith("pesos uruguayos "))
		self.assertEqual(bill["document"]["footer"], uruguay.invoice_context(frappe._dict())["footer"])

	def test_a_foreign_card_payment_lands_in_the_instant_bucket(self):
		folio = self._stay()
		session = cashier.open_session(PROP_UY, 500)["name"]
		api.add_folio_payment(folio, "Tarjeta extranjera", 1000)
		api.add_folio_payment(folio, "Tarjeta local", 300)
		modes = sorted(row.mode for row in frappe.get_doc("Folio", folio).payments)
		self.assertEqual(modes, ["Tarjeta extranjera", "Tarjeta local"])
		totals = cashier._recompute_session_totals(session, persist=False)
		self.assertEqual((totals["upi"], totals["card"]), (1300.0, 0.0))


def _bcu_post(body: bytes = BCU_REPLY, fail: Exception | None = None):
	resp = MagicMock()
	resp.raw.read.side_effect = lambda n=None, **kw: body[:n]
	return MagicMock(side_effect=fail) if fail else MagicMock(return_value=resp)


class TestUruguayBcuRates(AuditTestCase):
	properties = (PROP_UY,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		self._stack.enter_context(patch("kamra_uy.localization.uruguay.nowdate", return_value=FRIDAY))
		make_uy_property()
		frappe.db.delete("Exchange Rate", {"property": PROP_UY})
		frappe.db.delete("Property Setting", {"property": PROP_UY, "setting_key": ["like", "fx_%"]})
		from kamra import settings
		settings.write_kv(PROP_UY, "fx_currencies", "USD, EUR")
		settings.write_kv(PROP_UY, "fx_margin_pct", 2)
		settings.clear_cache(PROP_UY)
		self.addCleanup(settings.clear_cache, PROP_UY)
		for k in ("kamra:fx_not_offered:", "kamra:fx_market:"):
			frappe.cache.delete_keys(k)
			self.addCleanup(frappe.cache.delete_keys, k)

	def row(self, cur):
		return frappe.db.get_value("Exchange Rate", {"property": PROP_UY, "currency": cur},
		                           ["buy_rate", "sell_rate", "market_rate", "source"], as_dict=True)

	def test_fetch_stores_the_previous_close_with_the_margin(self):
		post = _bcu_post()
		with patch("requests.post", post), patch("requests.get") as open_feed:
			out = ledger._fetch_rates(PROP_UY)
		open_feed.assert_not_called()
		self.assertEqual(sorted(out["updated"]), ["EUR", "USD"])
		self.assertEqual(out["as_of"], "2026-11-12")
		usd = self.row("USD")
		self.assertEqual(usd.source, "Online")
		self.assertAlmostEqual(usd.market_rate, 40.589, places=9)
		self.assertAlmostEqual(usd.buy_rate, round(40.589 * 0.98, 9), places=9)
		self.assertAlmostEqual(usd.sell_rate, round(40.589 * 1.02, 9), places=9)
		self.assertAlmostEqual(self.row("EUR").market_rate, 44.850845, places=9)
		# la llamada: el servicio del BCU, sin redirecciones, con tope de tiempo, 10 días hacia atrás
		args, kwargs = post.call_args
		self.assertEqual(args[0], bcu.BCU_URL)
		self.assertEqual(kwargs["timeout"], 5)
		self.assertIs(kwargs["allow_redirects"], False)
		self.assertEqual(kwargs["headers"]["SOAPAction"], bcu.SOAP_ACTION)
		self.assertIn(b"<cot:FechaDesde>2026-11-03</cot:FechaDesde>", kwargs["data"])
		self.assertIn(b"<cot:FechaHasta>2026-11-13</cot:FechaHasta>", kwargs["data"])
		self.assertIn(b"<cot:item>2222</cot:item>", kwargs["data"])

	def test_the_accountant_changes_side_and_day_without_code(self):
		conf = {"kamra_uy_bcu": {"side": "TCV", "day_offset": 0, "codes": {"USD": 2222}}}
		with patch.dict(frappe.conf, conf), patch("requests.post", _bcu_post()):
			rates, as_of = uruguay.market_rates("UYU")
		self.assertEqual(rates, {"USD": 40.611})
		self.assertEqual(as_of, "2026-11-13")

	def test_a_dollar_home_gets_cross_rates(self):
		with patch("requests.post", _bcu_post()):
			rates, _ = uruguay.market_rates("USD")
		self.assertAlmostEqual(rates["UYU"], 1 / 40.589, places=9)
		self.assertNotIn("USD", rates)

	def test_todays_manual_rate_is_kept(self):
		ledger.upsert_exchange_rate(PROP_UY, "USD", 39.5, 41.5)
		with patch("requests.post", _bcu_post()):
			out = ledger._fetch_rates(PROP_UY)
		self.assertEqual(out["kept_manual"], ["USD"])
		self.assertEqual((self.row("USD").buy_rate, self.row("USD").source), (39.5, "Manual"))
		self.assertEqual(self.row("EUR").source, "Online")

	def test_failures_point_to_the_currency_desk(self):
		import requests

		for post in (_bcu_post(fail=requests.Timeout("read timed out")),
		             _bcu_post(BCU_REPLY.replace(b"<status>1</status>", b"<status>0</status>")),
		             _bcu_post(b"x" * (300 * 1024)),
		             _bcu_post(b"<r><respuestastatus><status>1</status></respuestastatus>"
		                       b"<datoscotizaciones/></r>")):
			with self.subTest(post=post), patch("requests.post", post), \
					self.assertRaises(frappe.ValidationError) as cm:
				ledger._fetch_rates(PROP_UY)
			self.assertIn("type them in on the Currency Desk", str(cm.exception))
		self.assertFalse(frappe.db.exists("Exchange Rate", {"property": PROP_UY}))
