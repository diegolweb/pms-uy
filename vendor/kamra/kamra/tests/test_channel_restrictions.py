"""Per-date stay restrictions (min stay on arrival / through, max stay,
closed to arrival / departure, stop sell): one rule set read the same way by
the booking engine, the front desk and the channel managers."""

from datetime import date, timedelta
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, nowdate

from kamra import channel_manager as cm
from kamra.pricing import stay_rules_by_day, stay_season_rules
from kamra.public_api import search_stay


def _next_weekday(weekday: int, after: int = 3) -> str:
	"""The first date on `weekday` (0 = Monday) at least `after` days ahead."""
	d = getdate(add_days(nowdate(), after))
	return str(d + timedelta(days=(weekday - d.weekday()) % 7))


def _next_dec_28() -> str:
	today = getdate(nowdate())
	d = date(today.year, 12, 28)
	return str(d if d >= today else date(today.year + 1, 12, 28))


class RestrictionFixture(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		frappe.local.lang = "en"
		self.property = frappe.get_doc({"doctype": "Property",
			"property_name": "TEST RESTR " + frappe.generate_hash(length=8),
			"country": "Uruguay", "currency": "USD",
			"booking_engine_enabled": 1}).insert(ignore_permissions=True).name
		self.twin = self.room_type("TWN", "Twin")
		self.double = self.room_type("DBL", "Double")
		self.bar = frappe.get_doc({"doctype": "Rate Plan", "property": self.property,
			"rate_plan_name": "Best available", "code": "BAR", "is_default": 1}).insert(
			ignore_permissions=True).name
		self.bb = frappe.get_doc({"doctype": "Rate Plan", "property": self.property,
			"rate_plan_name": "Bed and breakfast", "code": "BB"}).insert(
			ignore_permissions=True).name
		self.day = add_days(nowdate(), 10)

	def room_type(self, code: str, label: str) -> str:
		rt = frappe.get_doc({"doctype": "Room Type", "property": self.property,
			"room_type_code": code, "room_type_name": label, "base_price": 100,
			"adults_capacity": 2}).insert(ignore_permissions=True).name
		frappe.get_doc({"doctype": "Room", "property": self.property, "room_type": rt,
			"room_number": code + "1"}).insert(ignore_permissions=True)
		return rt

	def restriction(self, **kw):
		doc = {"doctype": "Channel Restriction", "property": self.property,
			"date_from": self.day, "date_to": self.day}
		doc.update(kw)
		return frappe.get_doc(doc).insert(ignore_permissions=True)

	def season(self, minimum_nights: int, **kw):
		doc = {"doctype": "Season", "property": self.property,
			"season_name": "TEST Peak " + frappe.generate_hash(length=6),
			"start_date": self.day, "end_date": self.day, "adjustment_type": "Percent",
			"adjustment_value": 0, "minimum_nights": minimum_nights, "priority": 10}
		doc.update(kw)
		return frappe.get_doc(doc).insert(ignore_permissions=True)

	def day_rules(self, day=None, room_type=None, rate_plan=None):
		day = str(day or self.day)
		return stay_rules_by_day(self.property, room_type, rate_plan, day, day)[day]


# --------------------------------------------------------------------- A.1
class ChannelRestrictionDoctypeTestCase(RestrictionFixture):
	def test_a_valid_row_is_saved(self):
		doc = self.restriction(room_type=self.twin, rate_plan=self.bb, min_stay_arrival=3,
			days_of_week="fri, sat", note="New Year")
		self.assertTrue(frappe.db.exists("Channel Restriction", doc.name))

	def test_the_end_date_cannot_be_before_the_start(self):
		with self.assertRaisesRegex(frappe.ValidationError, "end date cannot be before"):
			self.restriction(date_to=add_days(self.day, -1), stop_sell=1)

	def test_a_row_without_any_restriction_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Set at least one restriction"):
			self.restriction(note="nothing")
		with self.assertRaisesRegex(frappe.ValidationError, "Set at least one restriction"):
			self.restriction(min_stay_arrival=1, min_stay_through=1)

	def test_numbers_cannot_be_negative(self):
		for field in ("min_stay_arrival", "min_stay_through", "max_stay"):
			with self.assertRaisesRegex(frappe.ValidationError, "cannot be negative"):
				self.restriction(stop_sell=1, **{field: -1})

	def negative_message(self, lang: str, field: str) -> str:
		frappe.local.lang = lang
		try:
			with self.assertRaises(frappe.ValidationError) as caught:
				self.restriction(stop_sell=1, **{field: -1})
		finally:
			frappe.local.lang = "en"
		return str(caught.exception)

	# HR-161: the field inside «{0} cannot be negative.» carries the name the
	# restrictions screen gives it, in the desk's language.
	def test_a_negative_number_names_its_field_in_spanish(self):
		for field, message in (
			("max_stay", "Estadía máx. no admite valores negativos."),
			("min_stay_arrival", "Estadía mín. al llegar no admite valores negativos."),
			("min_stay_through", "Estadía mín. durante no admite valores negativos."),
		):
			with self.subTest(field=field):
				self.assertEqual(self.negative_message("es", field), message)

	def test_a_negative_number_names_its_field_in_portuguese(self):
		for field, message in (
			("max_stay", "Estadia máx. não aceita valores negativos."),
			("min_stay_arrival", "Estadia mín. na chegada não aceita valores negativos."),
			("min_stay_through", "Estadia mín. durante não aceita valores negativos."),
		):
			with self.subTest(field=field):
				self.assertEqual(self.negative_message("pt", field), message)

	def test_the_maximum_stay_cannot_be_shorter_than_the_minimum(self):
		with self.assertRaisesRegex(frappe.ValidationError, "maximum stay"):
			self.restriction(min_stay_through=4, max_stay=3)
		with self.assertRaisesRegex(frappe.ValidationError, "maximum stay"):
			self.restriction(min_stay_arrival=5, max_stay=2)
		self.restriction(min_stay_arrival=3, max_stay=3)

	def test_room_type_and_rate_plan_come_from_the_same_property(self):
		other = frappe.get_doc({"doctype": "Property",
			"property_name": "TEST RESTR B " + frappe.generate_hash(length=8)}).insert(
			ignore_permissions=True).name
		other_rt = frappe.get_doc({"doctype": "Room Type", "property": other,
			"room_type_code": "TWN", "room_type_name": "Twin", "base_price": 100,
			"adults_capacity": 2}).insert(ignore_permissions=True).name
		other_plan = frappe.get_doc({"doctype": "Rate Plan", "property": other,
			"rate_plan_name": "Other", "code": "OTH"}).insert(ignore_permissions=True).name
		with self.assertRaisesRegex(frappe.ValidationError, "Room type .* another property"):
			self.restriction(stop_sell=1, room_type=other_rt)
		with self.assertRaisesRegex(frappe.ValidationError, "Rate plan .* another property"):
			self.restriction(stop_sell=1, rate_plan=other_plan)


# --------------------------------------------------------------------- A.2
class StayRulesByDayTestCase(RestrictionFixture):
	def test_the_defaults_are_one_one_zero_and_open(self):
		self.assertEqual(self.day_rules(), {"min_stay_arrival": 1, "min_stay_through": 1,
			"max_stay": 0, "closed_to_arrival": False, "closed_to_departure": False,
			"stop_sell": False, "rule": None})

	def test_the_highest_minimum_wins_and_names_its_rule(self):
		self.season(2)
		r = self.restriction(min_stay_through=3)
		rules = self.day_rules()
		self.assertEqual(rules["min_stay_through"], 3)
		self.assertEqual(rules["rule"], f"Restriction {r.name}")

	def test_a_season_minimum_is_a_through_minimum_and_names_the_season(self):
		s = self.season(2)
		rules = self.day_rules()
		self.assertEqual((rules["min_stay_through"], rules["rule"]), (2, s.season_name))

	def test_the_property_floor_counts_unless_asked_not_to(self):
		frappe.db.set_value("Property", self.property, "minimum_nights", 2)
		self.assertEqual(self.day_rules()["min_stay_through"], 2)
		day = str(self.day)
		self.assertEqual(stay_rules_by_day(self.property, None, None, day, day,
			property_floor=False)[day]["min_stay_through"], 1)

	def test_a_blank_room_type_or_plan_covers_all_and_a_plan_only_its_own(self):
		self.restriction(stop_sell=1)
		for rt in (self.twin, self.double):
			for plan in (self.bar, self.bb):
				self.assertTrue(self.day_rules(room_type=rt, rate_plan=plan)["stop_sell"])
		self.restriction(rate_plan=self.bb, min_stay_arrival=4)
		self.assertEqual(self.day_rules(room_type=self.twin, rate_plan=self.bb)["min_stay_arrival"], 4)
		self.assertEqual(self.day_rules(room_type=self.twin, rate_plan=self.bar)["min_stay_arrival"], 1)
		self.restriction(room_type=self.double, closed_to_arrival=1)
		self.assertTrue(self.day_rules(room_type=self.double)["closed_to_arrival"])
		self.assertFalse(self.day_rules(room_type=self.twin)["closed_to_arrival"])

	def test_days_of_week_only_touch_those_days(self):
		friday = _next_weekday(4)
		self.restriction(date_from=friday, date_to=add_days(friday, 6),
			days_of_week="fri, sat", closed_to_arrival=1)
		rules = stay_rules_by_day(self.property, self.twin, None, friday, add_days(friday, 6))
		closed = [d for d, r in rules.items() if r["closed_to_arrival"]]
		self.assertEqual(closed, [friday, str(add_days(friday, 1))])

	def test_the_shortest_maximum_wins_and_zero_means_none(self):
		self.restriction(max_stay=5)
		self.restriction(max_stay=4)
		self.assertEqual(self.day_rules()["max_stay"], 4)
		self.restriction(stop_sell=1, max_stay=0)
		self.assertEqual(self.day_rules()["max_stay"], 4)

	def test_a_stay_reads_arrival_departure_and_each_night(self):
		ci = self.day
		co = add_days(ci, 3)
		self.restriction(date_from=ci, date_to=ci, closed_to_arrival=1)
		self.restriction(date_from=co, date_to=co, closed_to_departure=1)
		self.restriction(date_from=add_days(ci, 1), date_to=add_days(ci, 1), stop_sell=1)
		rules = stay_season_rules(self.property, ci, co, self.twin)
		self.assertTrue(rules["closed_to_arrival"])
		self.assertTrue(rules["closed_to_departure"])
		self.assertTrue(rules["stop_sell"])

	def test_departure_is_the_checkout_day_not_the_last_night(self):
		ci = self.day
		self.restriction(date_from=add_days(ci, 2), date_to=add_days(ci, 2), closed_to_departure=1)
		self.assertFalse(stay_season_rules(self.property, ci, add_days(ci, 3), self.twin)
			["closed_to_departure"])
		self.assertTrue(stay_season_rules(self.property, ci, add_days(ci, 2), self.twin)
			["closed_to_departure"])

	def test_the_stay_minimum_takes_arrival_and_through_and_keeps_the_season(self):
		s = self.season(2, end_date=add_days(self.day, 5))
		r = self.restriction(min_stay_arrival=4, max_stay=6)
		rules = stay_season_rules(self.property, self.day, add_days(self.day, 2), self.twin)
		self.assertEqual(rules["minimum_nights"], 4)
		self.assertEqual(rules["minimum_rule"], f"Restriction {r.name}")
		self.assertEqual(rules["max_stay"], 6)
		self.assertEqual(rules["season"], s.season_name)
		# an arrival minimum the stay does not arrive on does not count
		later = stay_season_rules(self.property, add_days(self.day, -1),
			add_days(self.day, 1), self.twin)
		self.assertEqual(later["minimum_nights"], 2)

	def test_no_rule_keeps_minimum_nights_at_zero(self):
		rules = stay_season_rules(self.property, self.day, add_days(self.day, 1), self.twin)
		self.assertEqual(rules["minimum_nights"], 0)
		self.assertIsNone(rules["minimum_rule"])

	def test_no_plan_means_the_default_plan(self):
		self.restriction(rate_plan=self.bar, stop_sell=1)
		self.assertTrue(self.day_rules()["stop_sell"])
		self.assertTrue(stay_season_rules(self.property, self.day, add_days(self.day, 1))["stop_sell"])
		self.assertFalse(self.day_rules(rate_plan=self.bb)["stop_sell"])

	def test_a_disabled_row_is_ignored(self):
		self.restriction(stop_sell=1, disabled=1)
		self.assertFalse(self.day_rules()["stop_sell"])

	def test_five_hundred_days_take_at_most_three_queries(self):
		frappe.db.set_value("Property", self.property, "minimum_nights", 2)
		self.season(2, end_date=add_days(self.day, 100))
		self.restriction(date_to=add_days(self.day, 300), rate_plan=self.bar, min_stay_arrival=3)
		self.restriction(date_to=add_days(self.day, 200), stop_sell=1, days_of_week="sat")
		real = frappe.db.sql
		with patch.object(frappe.db, "sql", side_effect=real) as sql:
			rules = stay_rules_by_day(self.property, self.twin, None, nowdate(),
				add_days(nowdate(), 499))
		self.assertEqual(len(rules), 500)
		self.assertLessEqual(sql.call_count, 3)
		self.assertEqual(rules[str(self.day)]["min_stay_arrival"], 3)

	def test_quote_carries_the_new_stay_rules(self):
		from kamra.pricing import quote
		self.restriction(rate_plan=self.bb, closed_to_arrival=1)
		q = quote(self.property, self.twin, self.day, add_days(self.day, 2), rate_plan=self.bb)
		for key in ("minimum_nights", "season", "minimum_rule", "max_stay",
		            "closed_to_arrival", "closed_to_departure", "stop_sell"):
			self.assertIn(key, q["stay_rules"])
		self.assertTrue(q["stay_rules"]["closed_to_arrival"])
		self.assertFalse(quote(self.property, self.twin, self.day, add_days(self.day, 2),
			rate_plan=self.bar)["stay_rules"]["closed_to_arrival"])


# --------------------------------------------------------------------- A.3
class DirectSaleRespectsRestrictionsTestCase(RestrictionFixture):
	def row(self, ci, co, room_type=None):
		rows = search_stay(self.property, str(ci), str(co), 2, 0)
		return next(r for r in rows if r["room_type"] == (room_type or self.twin))

	def test_a_new_year_arrival_minimum_turns_a_two_night_stay_away(self):
		dec28 = _next_dec_28()
		self.restriction(date_from=dec28, date_to=add_days(dec28, 5), min_stay_arrival=3)
		short = self.row(dec28, add_days(dec28, 2))
		self.assertFalse(short["available"])
		self.assertIn("at least 3 nights", short["reason"])
		self.assertTrue(self.row(dec28, add_days(dec28, 3))["available"])

	def test_each_closed_rule_turns_the_stay_away_with_its_reason(self):
		ci = self.day
		cases = (
			({"date_from": ci, "date_to": ci, "closed_to_arrival": 1}, 2, "Arrivals are not possible"),
			({"date_from": add_days(ci, 2), "date_to": add_days(ci, 2), "closed_to_departure": 1}, 2,
			 "Departures are not possible"),
			({"date_from": add_days(ci, 1), "date_to": add_days(ci, 1), "stop_sell": 1}, 2,
			 "closed to new bookings"),
			({"date_from": ci, "date_to": ci, "max_stay": 4}, 5, "at most 4 nights"),
		)
		for kw, nights, reason in cases:
			with self.subTest(reason=reason):
				r = self.restriction(**kw)
				row = self.row(ci, add_days(ci, nights))
				self.assertFalse(row["available"])
				self.assertIn(reason, row["reason"])
				r.delete(ignore_permissions=True)
				self.assertTrue(self.row(ci, add_days(ci, nights))["available"])

	# HR-128: a maximum stay of one night is said in the singular, in every language; two or more, in the plural.
	MAX_STAY_SAYS = {
		"en": ("These dates allow a stay of at most 1 night.", "These dates allow a stay of at most 2 nights."),
		"es": ("Para estas fechas, la estadía máxima es de 1 noche.",
		       "Para estas fechas, la estadía máxima es de 2 noches."),
		"pt": ("Para essas datas, a estadia máxima é de 1 noite.", "Para essas datas, a estadia máxima é de 2 noites."),
	}

	def speak(self, lang: str):
		from frappe.translate import clear_cache
		clear_cache()
		frappe.local.lang = lang

	def test_the_booking_page_says_one_night_in_the_singular(self):
		for lang, sentences in self.MAX_STAY_SAYS.items():
			for max_stay, sentence in zip((1, 2), sentences):
				with self.subTest(lang=lang, max_stay=max_stay):
					self.speak(lang)
					r = self.restriction(max_stay=max_stay)
					try:
						self.assertEqual(self.row(self.day, add_days(self.day, max_stay + 1))["reason"], sentence)
					finally:
						r.delete(ignore_permissions=True)

	def test_a_website_booking_over_the_maximum_says_one_night_in_the_singular(self):
		for lang, sentences in self.MAX_STAY_SAYS.items():
			for max_stay, sentence in zip((1, 2), sentences):
				with self.subTest(lang=lang, max_stay=max_stay):
					self.speak(lang)
					r = self.restriction(max_stay=max_stay)
					try:
						with self.assertRaises(frappe.ValidationError) as refused:
							self.reservation("Website", nights=max_stay + 1)
						self.assertEqual(str(refused.exception), sentence)
					finally:
						r.delete(ignore_permissions=True)

	def test_a_platform_booking_over_the_maximum_notes_one_night_in_the_singular(self):
		says = {"en": ("maximum stay of 1 night", "maximum stay of 2 nights"),
		        "es": ("estadía máxima de 1 noche", "estadía máxima de 2 noches"),
		        "pt": ("estadia máxima de 1 noite", "estadia máxima de 2 noites")}
		# each booking on its own dates: the twin has one room, and a platform booking stays booked
		arrivals = iter(range(0, 60, 5))
		for lang, notes in says.items():
			for max_stay, note in zip((1, 2), notes):
				with self.subTest(lang=lang, max_stay=max_stay):
					self.speak(lang)
					day = add_days(self.day, next(arrivals))
					self.restriction(date_from=day, date_to=day, max_stay=max_stay)
					res = self.reservation("OTA", nights=max_stay + 1, channel="Booking.com",
						check_in_date=day, check_out_date=add_days(day, max_stay + 1))
					self.assertTrue((res.special_requests or "").endswith(note), res.special_requests)

	def test_a_maximum_of_one_night_under_the_minimum_is_refused_in_the_singular(self):
		says = {"en": ("The maximum stay (1 night) cannot be shorter than the minimum stay (3 nights).",
		               "The maximum stay (2 nights) cannot be shorter than the minimum stay (3 nights)."),
		        "es": ("La estadía máxima (1 noche) no puede ser menor que la mínima (3 noches).",
		               "La estadía máxima (2 noches) no puede ser menor que la mínima (3 noches)."),
		        "pt": ("A estadia máxima (1 noite) não pode ser menor que a mínima (3 noites).",
		               "A estadia máxima (2 noites) não pode ser menor que a mínima (3 noites).")}
		for lang, sentences in says.items():
			for max_stay, sentence in zip((1, 2), sentences):
				with self.subTest(lang=lang, max_stay=max_stay):
					self.speak(lang)
					with self.assertRaises(frappe.ValidationError) as refused:
						self.restriction(min_stay_through=3, max_stay=max_stay)
					self.assertEqual(str(refused.exception), sentence)

	def test_a_restriction_of_another_plan_leaves_the_direct_sale_open(self):
		self.restriction(rate_plan=self.bb, stop_sell=1)
		self.assertTrue(self.row(self.day, add_days(self.day, 2))["available"])

	def reservation(self, source: str, nights: int = 2, **kw):
		guest = frappe.get_doc({"doctype": "Guest", "first_name": "Restr",
			"last_name": frappe.generate_hash(length=6)}).insert(ignore_permissions=True).name
		doc = {"doctype": "Reservation", "property": self.property, "room_type": self.twin,
			"guest": guest, "status": "Confirmed", "source": source,
			"check_in_date": self.day, "check_out_date": add_days(self.day, nights), "adults": 2}
		doc.update(kw)
		return frappe.get_doc(doc).insert(ignore_permissions=True)

	def test_a_website_booking_on_a_closed_arrival_is_refused(self):
		self.restriction(closed_to_arrival=1)
		with self.assertRaisesRegex(frappe.ValidationError, "Arrivals are not possible"):
			self.reservation("Website")

	def test_the_front_desk_can_book_over_a_closed_arrival_with_a_warning(self):
		self.restriction(closed_to_arrival=1)
		frappe.clear_messages()
		res = self.reservation("Manual")
		self.assertTrue(frappe.db.exists("Reservation", res.name))
		messages = " ".join(str(m) for m in frappe.get_message_log())
		self.assertIn("Arrivals are not possible", messages)

	def test_the_front_desk_still_cannot_go_under_the_minimum_stay(self):
		self.restriction(min_stay_arrival=3)
		with self.assertRaisesRegex(frappe.ValidationError, "at least 3 nights"):
			self.reservation("Manual")

	def test_a_rule_added_later_does_not_refuse_an_unrelated_save(self):
		res = self.reservation("Manual")
		self.restriction(min_stay_arrival=3)
		res.reload()
		res.special_requests = "Quiet room"
		res.save(ignore_permissions=True)

	def test_a_platform_booking_is_never_refused_and_says_why(self):
		self.season(3, end_date=add_days(self.day, 5))
		res = self.reservation("OTA", channel="Booking.com")
		self.assertTrue(frappe.db.exists("Reservation", res.name))
		self.assertIn("Booked by Booking.com outside the stay rules", res.special_requests or "")
		self.assertIn("3", res.special_requests)
		self.assertEqual((res.special_requests or "").count("outside the stay rules"), 1)

	def test_a_platform_booking_through_the_channel_manager_is_booked(self):
		self.season(3, end_date=add_days(self.day, 5))
		conn = frappe.get_doc({"doctype": "Channel Manager Connection", "property": self.property,
			"provider": "Channex", "active": 1, "external_property_id": "restr"}).insert(
			ignore_permissions=True)
		frappe.get_doc({"doctype": "Channel Room Mapping", "connection": conn.name,
			"room_type": self.twin, "external_room_id": "twin"}).insert(ignore_permissions=True)
		tag = frappe.generate_hash(length=8)
		out = cm._apply_event(conn, {"event": "book", "ota_ref": "TEST-R-" + tag,
			"channel": "Booking.com", "room_type_external_id": "twin", "check_in": self.day,
			"check_out": str(add_days(self.day, 2)), "adults": 2, "children": 0,
			"guest_name": "Prueba Estancia", "phone": "", "email": f"r-{tag}@example.test",
			"total": 0, "nightly": [], "currency": "", "notes": ""})
		self.assertEqual(out["result"], "booked", out)
		notes = frappe.db.get_value("Reservation", out["reservation"], "special_requests")
		self.assertIn("outside the stay rules", notes or "")


# --------------------------------------------------------------------- A.4
class RestrictionScreenApiTestCase(RestrictionFixture):
	def rows(self, n=3):
		return [{"room_type": self.twin if i % 2 else "", "rate_plan": self.bb if i == 2 else "",
			"date_from": add_days(self.day, i), "date_to": add_days(self.day, i + 1),
			"min_stay_arrival": 2 + i} for i in range(n)]

	def test_save_creates_every_row_in_one_go(self):
		from kamra.channel_restrictions import list_restrictions, save_restrictions
		out = save_restrictions(self.property, frappe.as_json(self.rows()))
		self.assertEqual(len(out["saved"]), 3)
		listed = list_restrictions(self.property)
		self.assertEqual({r["name"] for r in listed}, set(out["saved"]))

	def test_one_bad_row_saves_none(self):
		from kamra.channel_restrictions import save_restrictions
		rows = self.rows()
		rows[1]["date_to"] = add_days(self.day, -5)
		with self.assertRaisesRegex(frappe.ValidationError, "end date cannot be before"):
			save_restrictions(self.property, rows)
		self.assertFalse(frappe.get_all("Channel Restriction", filters={"property": self.property}))

	def test_a_row_with_a_name_is_updated(self):
		from kamra.channel_restrictions import save_restrictions
		name = save_restrictions(self.property, self.rows(1))["saved"][0]
		save_restrictions(self.property, [{"name": name, "date_from": self.day,
			"date_to": self.day, "stop_sell": 1, "min_stay_arrival": 0}])
		doc = frappe.get_doc("Channel Restriction", name)
		self.assertEqual((doc.stop_sell, doc.min_stay_arrival), (1, 0))

	def test_a_row_of_another_property_is_not_touched(self):
		from kamra.channel_restrictions import delete_restrictions, save_restrictions
		mine = self.restriction(stop_sell=1).name
		other = frappe.get_doc({"doctype": "Property",
			"property_name": "TEST RESTR C " + frappe.generate_hash(length=8)}).insert(
			ignore_permissions=True).name
		self.assertEqual(delete_restrictions(other, [mine]), {"deleted": 0})
		self.assertTrue(frappe.db.exists("Channel Restriction", mine))
		with self.assertRaises(frappe.ValidationError):
			save_restrictions(other, [{"name": mine, "date_from": self.day,
				"date_to": self.day, "stop_sell": 1}])
		self.assertEqual(delete_restrictions(self.property, [mine]), {"deleted": 1})

	def test_list_filters_by_dates_and_room_type(self):
		from kamra.channel_restrictions import list_restrictions
		a = self.restriction(stop_sell=1).name
		b = self.restriction(room_type=self.double, date_from=add_days(self.day, 20),
			date_to=add_days(self.day, 22), closed_to_arrival=1).name
		self.assertEqual({r["name"] for r in list_restrictions(self.property,
			date_from=add_days(self.day, 21))}, {b})
		# a room type filter keeps the rows for all room types too
		self.assertEqual({r["name"] for r in list_restrictions(self.property,
			room_type=self.twin)}, {a})

	def test_the_front_desk_reads_but_cannot_save(self):
		from kamra.channel_restrictions import list_restrictions, save_restrictions
		from kamra.tests.audit_fixtures import ensure_user
		self.restriction(stop_sell=1)
		desk = ensure_user("ut.restr.desk@example.test", ["Front Desk"])
		frappe.set_user(desk)  # nosemgrep: frappe-setuser -- test persona
		try:
			self.assertEqual(len(list_restrictions(self.property)), 1)
			with self.assertRaises(frappe.PermissionError):
				save_restrictions(self.property, self.rows(1))
		finally:
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture


class RestrictionPropertyScopeTestCase(RestrictionFixture):
	"""HR-110: with kamra_strict_property_scope on, a Revenue Manager of another
	hotel of the chain can neither read, save nor delete this hotel's
	restrictions - through the screen API or Frappe's own REST reads."""

	def setUp(self):
		super().setUp()
		from kamra.tests.audit_fixtures import ensure_user
		self.mine = self.restriction(stop_sell=1).name
		self.other = frappe.get_doc({"doctype": "Property",
			"property_name": "TEST RESTR B " + frappe.generate_hash(length=8),
			"country": "Uruguay", "currency": "USD"}).insert(ignore_permissions=True).name
		self.theirs = frappe.get_doc({"doctype": "Channel Restriction", "property": self.other,
			"date_from": self.day, "date_to": self.day, "stop_sell": 1}).insert(
			ignore_permissions=True).name
		self.manager = ensure_user("ut.restr.rm.other@example.test", ["Revenue Manager"])
		self.only_hotel(self.other)
		strict = patch.dict(frappe.conf, {"kamra_strict_property_scope": 1})
		strict.start()
		self.addCleanup(strict.stop)
		self.addCleanup(frappe.set_user, "Administrator")  # nosemgrep: frappe-setuser -- test fixture

	def only_hotel(self, prop: str | None):
		frappe.db.delete("User Permission", {"user": self.manager, "allow": "Property"})
		if prop:
			frappe.get_doc({"doctype": "User Permission", "user": self.manager,
				"allow": "Property", "for_value": prop}).insert(ignore_permissions=True)
		frappe.clear_cache(user=self.manager)

	def as_manager(self):
		frappe.set_user(self.manager)  # nosemgrep: frappe-setuser -- the restricted persona

	def calls_on(self, prop: str):
		from kamra.channel_restrictions import delete_restrictions, list_restrictions, save_restrictions
		row = {"date_from": self.day, "date_to": self.day, "closed_to_arrival": 1}
		return {
			"list": lambda: list_restrictions(prop),
			"save": lambda: save_restrictions(prop, [row]),
			"delete": lambda: delete_restrictions(prop, [self.mine]),
		}

	def rest_names(self) -> set[str]:
		return set(frappe.get_list("Channel Restriction", pluck="name", limit_page_length=0,
			filters={"property": ["in", [self.property, self.other]]}))

	def assert_this_hotel_untouched(self):
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		self.assertEqual(frappe.get_all("Channel Restriction", pluck="name",
			filters={"property": self.property}), [self.mine])
		self.assertEqual(frappe.db.get_value("Channel Restriction", self.mine,
			["stop_sell", "closed_to_arrival"]), (1, 0))

	def test_the_manager_works_on_their_own_hotel(self):
		from kamra.channel_restrictions import delete_restrictions, list_restrictions, save_restrictions
		self.as_manager()
		self.assertEqual({r["name"] for r in list_restrictions(self.other)}, {self.theirs})
		saved = save_restrictions(self.other, [{"date_from": self.day, "date_to": self.day,
			"closed_to_arrival": 1}])["saved"]
		self.assertEqual(delete_restrictions(self.other, saved), {"deleted": 1})

	def test_list_save_and_delete_on_another_hotel_are_refused(self):
		self.as_manager()
		for what, call in self.calls_on(self.property).items():
			with self.subTest(what), self.assertRaises(frappe.PermissionError):
				call()
		self.assert_this_hotel_untouched()

	def test_their_own_hotel_with_this_hotels_row_touches_nothing(self):
		from kamra.channel_restrictions import delete_restrictions, save_restrictions
		self.as_manager()
		self.assertEqual(delete_restrictions(self.other, [self.mine]), {"deleted": 0})
		with self.assertRaises(frappe.ValidationError):
			save_restrictions(self.other, [{"name": self.mine, "date_from": self.day,
				"date_to": self.day, "stop_sell": 0, "closed_to_arrival": 1}])
		self.assert_this_hotel_untouched()

	def test_rest_does_not_list_or_read_another_hotels_restrictions(self):
		self.as_manager()
		self.assertEqual(self.rest_names(), {self.theirs})
		self.assertFalse(frappe.has_permission("Channel Restriction", "read", self.mine))
		with self.assertRaises(frappe.PermissionError):
			frappe.client.get("Channel Restriction", self.mine)

	def test_strict_scope_without_any_hotel_refuses_everything(self):
		self.only_hotel(None)
		self.as_manager()
		for prop in (self.property, self.other):
			for what, call in self.calls_on(prop).items():
				with self.subTest(prop=prop, call=what), self.assertRaises(frappe.PermissionError):
					call()
		self.assertEqual(self.rest_names(), set())
		self.assert_this_hotel_untouched()
