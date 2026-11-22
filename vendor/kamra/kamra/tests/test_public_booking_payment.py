"""The booking page takes its deposit online: /book returns the gateway's
payment link and holds the rooms until the link's window ends; the guest
comes back to /book?booking=<status token> and sees their booking's state;
"First night" asks for one night (the stay's total divided by its nights).

Runs with `bench run-tests --module kamra.tests.test_public_booking_payment`.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import add_to_date, get_datetime, get_system_timezone, now_datetime

from kamra import localization, payments, public_api
from kamra.reservation_state import expire_holds
from kamra.tests.audit_fixtures import AuditTestCase, d, make_property, make_room, make_room_type

PROP = "UT Web Deposit"
PHONE = "919800031031"


class WebDepositCase(AuditTestCase):
	properties = (PROP,)

	def setUp(self):
		super().setUp()
		frappe.local.lang = "en"
		# India: the one Kamra pack that offers gateways (Razorpay, Cashfree)
		make_property(PROP, country="India", booking_engine_enabled=1,
		              booking_payment_mode="First night", booking_mode="Instant",
		              hold_minutes=30, advance_percent=0, registration_fee=0,
		              cleaning_fee=0)
		self.rt = make_room_type(PROP, "WEB", base_price=1000, adults_capacity=2)
		make_room(PROP, self.rt, "W101")
		make_room(PROP, self.rt, "W102")
		self.ci, self.co = d(20), d(23)  # three nights
		for name in frappe.get_all("Payment Gateway Settings", {"property": PROP}, pluck="name"):
			frappe.delete_doc("Payment Gateway Settings", name, ignore_permissions=True, force=True)

	def gateway(self, **over):
		return frappe.get_doc({"doctype": "Payment Gateway Settings", "property": PROP,
		                       "gateway": "Razorpay", "enabled": 1, "test_mode": 1,
		                       **over}).insert(ignore_permissions=True).name

	def mode(self, mode: str):
		frappe.db.set_value("Property", PROP, "booking_payment_mode", mode)
		frappe.clear_document_cache("Property", PROP)

	def season(self, **kw):
		return frappe.get_doc({"doctype": "Season", "property": PROP,
			"season_name": "UT High " + frappe.generate_hash(length=6),
			"start_date": self.ci, "end_date": self.co, "adjustment_type": "Percent",
			"adjustment_value": 0, "minimum_nights": 0, "priority": 10, **kw}).insert(
			ignore_permissions=True)

	def row(self, ci=None, co=None):
		return public_api.search_stay(PROP, ci or self.ci, co or self.co, 2, 0)[0]

	def book(self, **kw):
		args = {"property": PROP, "room_type": self.rt, "check_in_date": self.ci,
		        "check_out_date": self.co, "guest_name": "UT Web Guest", "phone": PHONE,
		        "email": "web-guest@example.com", "adults": 2}
		args.update(kw)
		return public_api.book(**args)

	def folio(self, reservation):
		return frappe.db.get_value("Folio", {"reservation": reservation, "folio_type": "Guest"})


class TestFirstNight(WebDepositCase):
	def test_first_night_asks_for_the_total_divided_by_the_nights_in_every_plan(self):
		row = self.row()
		total = row["quote"]["amount_after_tax"]
		self.assertGreater(total, 0)
		self.assertEqual(row["quote"]["advance_due"], round(total / 3, 2))
		for plan in row["plans"]:
			self.assertEqual(plan["advance_due"], round(plan["amount_after_tax"] / 3, 2))
			self.assertEqual(plan["balance_at_hotel"],
			                 round(plan["amount_after_tax"] - plan["advance_due"], 2))
		self.assertIn("One night", row["quote"]["payment_policy"])

	def test_a_season_with_its_own_advance_percent_wins_over_first_night(self):
		self.season(advance_percent=50)
		row = self.row()
		total = row["quote"]["amount_after_tax"]
		self.assertEqual(row["quote"]["advance_due"], round(total * 0.5, 2))

	def test_pay_at_hotel_asks_for_nothing(self):
		self.mode("Pay at hotel")
		self.assertEqual(self.row()["quote"]["advance_due"], 0)

	def test_one_night_stay_asks_for_the_whole_night(self):
		row = self.row(self.ci, d(21))
		self.assertEqual(row["quote"]["advance_due"], row["quote"]["amount_after_tax"])

	def test_the_booking_keeps_one_night_as_its_deposit(self):
		out = self.book()
		self.assertEqual(out["advance_due"], round(out["amount_after_tax"] / 3, 2))
		self.assertEqual(float(frappe.db.get_value("Reservation", out["reservation"], "advance_due")),
		                 out["advance_due"])

	def test_first_night_is_an_option_of_the_property(self):
		options = frappe.get_meta("Property").get_field("booking_payment_mode").options.split("\n")
		self.assertIn("First night", options)
		from kamra.settings import SETTINGS
		_, field = SETTINGS["booking_payment_mode"]
		self.assertIn("First night", field.options)


class TestGatewayReturnUrl(WebDepositCase):
	def providers(self):
		took, plain = [], []

		def with_return(settings, *, amount, currency, reference, description, customer, notes,
		                return_url=None):
			took.append(return_url)
			return "l1", "https://pay.example.test/l1"

		def without(settings, *, amount, currency, reference, description, customer, notes):
			plain.append(reference)
			return "l2", "https://pay.example.test/l2"

		return took, plain, SimpleNamespace(create_link=with_return), SimpleNamespace(create_link=without)

	def link(self):
		return {"amount": 10.0, "currency": "INR", "reference": "F-1", "description": "d",
		        "customer": {}, "notes": {}}

	def test_a_provider_that_takes_a_return_url_gets_it(self):
		took, _, provider, _ = self.providers()
		with patch.object(localization, "_provider", return_value=provider):
			localization.create_gateway_link(None, SimpleNamespace(gateway="X"),
			                                 return_url="https://x.example/r", **self.link())
		self.assertEqual(took, ["https://x.example/r"])

	def test_a_provider_without_one_keeps_its_own_page(self):
		_, plain, _, provider = self.providers()
		with patch.object(localization, "_provider", return_value=provider):
			out = localization.create_gateway_link(None, SimpleNamespace(gateway="X"),
			                                       return_url="https://x.example/r", **self.link())
		self.assertEqual((plain, out), (["F-1"], ("l2", "https://pay.example.test/l2")))

	def test_create_payment_link_hands_the_return_url_to_the_gateway(self):
		self.gateway(test_mode=0, key_id="ut_key", key_secret="ut-not-a-secret")
		with patch.object(localization, "create_gateway_link",
		                  return_value=("l3", "https://pay.example.test/l3")) as made:
			folio = self.folio(self.book()["reservation"])
			payments.create_payment_link(folio, amount=10, return_url="https://x.example/r")
		self.assertEqual(made.call_args.kwargs["return_url"], "https://x.example/r")


class TestBookTakesTheDeposit(WebDepositCase):
	def test_book_returns_the_payment_link_and_a_status_token(self):
		self.gateway()
		before = now_datetime()
		out = self.book()
		self.assertEqual(out["status"], "Pending Payment")
		self.assertTrue(out["payment_url"])
		self.assertEqual(out["payment_gateway"], "Razorpay")
		self.assertIs(out["payment_error"], False)
		self.assertEqual(public_api._res_by_status_token(out["status_token"]).name, out["reservation"])
		expires = datetime.fromisoformat(out["payment_expires_on"])
		self.assertIsNotNone(expires.tzinfo)  # the guest's browser shows it in their own time
		hold = get_datetime(frappe.db.get_value("Reservation", out["reservation"], "hold_expires_on"))
		self.assertEqual(expires, hold.replace(microsecond=0, tzinfo=ZoneInfo(get_system_timezone())))
		self.assertAlmostEqual((hold - before).total_seconds(), 30 * 60, delta=120)
		folio = frappe.db.get_value("Folio", self.folio(out["reservation"]),
		                            ["payment_link_url", "payment_link_amount"], as_dict=True)
		self.assertEqual(folio.payment_link_url, out["payment_url"])
		self.assertEqual(float(folio.payment_link_amount), out["advance_due"])

	def test_an_online_hold_lasts_at_least_ten_minutes(self):
		self.gateway()
		frappe.db.set_value("Property", PROP, "hold_minutes", 5)
		frappe.clear_document_cache("Property", PROP)
		before = now_datetime()
		out = self.book()
		hold = get_datetime(frappe.db.get_value("Reservation", out["reservation"], "hold_expires_on"))
		self.assertGreaterEqual((hold - before).total_seconds(), 10 * 60 - 5)

	def test_status_before_and_after_the_deposit_is_paid(self):
		self.gateway()
		out = self.book()
		before = public_api.booking_status(out["status_token"])
		self.assertEqual(before["status"], "Pending Payment")
		self.assertTrue(before["payment_url"])
		self.assertIs(before["expired"], False)
		self.assertEqual(before["nights"], 3)
		folio = self.folio(out["reservation"])
		link_id = frappe.db.get_value("Folio", folio, "payment_link_id")
		self.assertTrue(payments.settle_payment_link(folio, link_id, out["advance_due"]))
		after = public_api.booking_status(out["status_token"])
		self.assertEqual(after["status"], "Confirmed")
		self.assertEqual(after["advance_paid"], out["advance_due"])
		self.assertIsNone(after["payment_url"])

	def test_an_expired_hold_says_so_releases_the_room_and_takes_no_new_link(self):
		self.gateway()
		left = self.row()["rooms_left"]
		out = self.book()
		self.assertEqual(self.row()["rooms_left"], left - 1)
		frappe.db.set_value("Reservation", out["reservation"], "hold_expires_on",
		                    add_to_date(now_datetime(), minutes=-1))
		expire_holds()
		status = public_api.booking_status(out["status_token"])
		self.assertEqual(status["status"], "Cancelled")
		self.assertIs(status["expired"], True)
		self.assertIsNone(status["payment_url"])
		with self.assertRaises(frappe.ValidationError):
			public_api.booking_payment_link(out["status_token"])
		self.assertEqual(self.row()["rooms_left"], left)

	def test_a_new_link_reuses_the_folio_link_while_the_deposit_is_unchanged(self):
		self.gateway()
		out = self.book()
		again = public_api.booking_payment_link(out["status_token"])
		self.assertEqual(again["payment_url"], out["payment_url"])
		self.assertEqual(again["payment_expires_on"], out["payment_expires_on"])

	def test_a_link_failure_keeps_the_hold_and_retry_makes_one(self):
		self.gateway()
		with patch.object(payments, "create_payment_link", side_effect=frappe.ValidationError("down")):
			out = self.book()
		self.assertEqual((out["status"], out["payment_url"], out["payment_error"]),
		                 ("Pending Payment", None, True))
		self.assertTrue(public_api.booking_payment_link(out["status_token"])["payment_url"])

	def test_pay_at_hotel_confirms_with_no_link(self):
		self.gateway()
		self.mode("Pay at hotel")
		out = self.book()
		self.assertEqual((out["status"], out["payment_url"], out["payment_error"]),
		                 ("Confirmed", None, False))

	def test_without_a_gateway_the_booking_is_held_as_today(self):
		out = self.book()
		self.assertEqual((out["status"], out["payment_url"], out["payment_error"],
		                  out["payment_gateway"]), ("Pending Payment", None, False, None))
		self.assertIsNone(public_api.showcase(PROP)["property"]["payment_gateway"])

	def test_the_showcase_names_the_gateway(self):
		self.gateway()
		self.assertEqual(public_api.showcase(PROP)["property"]["payment_gateway"], "Razorpay")

	def test_a_return_url_on_an_unlisted_origin_books_nothing(self):
		self.gateway()
		before = frappe.db.count("Reservation", {"property": PROP})
		with self.assertRaises(frappe.ValidationError):
			self.book(return_url="https://evil.example/x")
		self.assertEqual(frappe.db.count("Reservation", {"property": PROP}), before)

	def test_a_listed_origin_gets_the_guest_back_with_the_token(self):
		self.gateway()
		with patch.dict(frappe.conf, {"booking_return_origins": ["https://web.example.test"]}):
			url = public_api._return_url("https://web.example.test/reservar/gracias", "R-1.abc")
			with self.assertRaises(frappe.ValidationError):
				public_api._return_url("https://web.example.test.evil.example/x", "R-1.abc")
			with self.assertRaises(frappe.ValidationError):
				public_api._return_url("javascript:alert(1)", "R-1.abc")
		parts = urlsplit(url)
		self.assertEqual((parts.scheme, parts.netloc, parts.path),
		                 ("https", "web.example.test", "/reservar/gracias"))
		self.assertEqual(parse_qs(parts.query)["booking"], ["R-1.abc"])
		own = urlsplit(public_api._return_url("", "R-1.abc"))
		self.assertEqual(own.path, "/kamra/book")
		self.assertEqual(parse_qs(own.query)["booking"], ["R-1.abc"])

	def test_the_payment_link_goes_back_to_the_booking_page(self):
		self.gateway(test_mode=0, key_id="ut_key", key_secret="ut-not-a-secret")
		with patch.object(localization, "create_gateway_link",
		                  return_value=("l4", "https://pay.example.test/l4")) as made:
			out = self.book()
		back = urlsplit(made.call_args.kwargs["return_url"])
		self.assertEqual(back.path, "/kamra/book")
		self.assertEqual(parse_qs(back.query)["booking"], [out["status_token"]])
		self.assertEqual(made.call_args.kwargs["amount"], out["advance_due"])


class TestStatusToken(WebDepositCase):
	def test_a_changed_signature_is_refused(self):
		self.gateway()
		token = self.book()["status_token"]
		name, _, sig = token.rpartition(".")
		bad = f"{name}.{('0' if sig[0] != '0' else '1')}{sig[1:]}"
		for wrong in (bad, f"{name}.", "nonsense", "", f"RES-NOPE.{sig}"):
			with self.assertRaises(frappe.PermissionError):
				public_api.booking_status(wrong)

	def test_the_status_says_nothing_about_the_guest(self):
		self.gateway()
		status = public_api.booking_status(self.book()["status_token"])
		self.assertEqual(set(status), {
			"reservation", "status", "expired", "property_name", "room_type_name", "check_in_date",
			"check_out_date", "nights", "currency", "amount_after_tax", "advance_due", "advance_paid",
			"balance_at_hotel", "hold_expires_on", "payment_url", "payment_gateway"})
		for key in status:
			self.assertFalse(any(w in key for w in ("guest", "phone", "email")), key)
		self.assertNotIn(PHONE, frappe.as_json(status))
		self.assertNotIn("web-guest@example.com", frappe.as_json(status))

	def test_the_status_token_is_not_the_precheckin_token(self):
		self.gateway()
		out = self.book()
		pre = frappe.db.get_value("Reservation", out["reservation"], "precheckin_token")
		self.assertNotIn(out["status_token"].rpartition(".")[2], pre or "")
		self.assertEqual(public_api.booking_status_token(out["reservation"]), out["status_token"])
