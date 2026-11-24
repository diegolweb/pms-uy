"""Stay restrictions out to Channex through the ARI outbox.

The restrictions the booking engine and the front desk already obey (Season
minimums, Channel Restriction rows, the property's floor: one
pricing.stay_rules_by_day) go to Channex in the same `restrictions` call as
the rate, each in its own field: min_stay_arrival, min_stay_through,
max_stay, closed_to_arrival, closed_to_departure and stop_sell - never the
`min_stay` shortcut a property with min_stay_type "both" refuses.

Each certification case is a test named after its number (test_cert_NN_...),
set from the screen's own API (save_restrictions) or a Season, as the PMS
does it. No HTTP: FakeChannex stands in for channex._send. Dates are
relative to today: the certification guide's own dates would expire."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate

from kamra.tests.test_channex_outbox import _AriFixture, _AriWorkerMixin, day

RULE_FIELDS = ("min_stay_arrival", "min_stay_through", "max_stay",
               "closed_to_arrival", "closed_to_departure", "stop_sell")


class _BarPlanFixture(_AriFixture):
	"""The outbox fixture with BAR as a real (default) Rate Plan, so a
	restriction can name it: the certification's Twin and Double, each
	with BAR and BB."""

	def setUp(self):
		super().setUp()
		frappe.db.set_value("Property", self.property, "booking_engine_enabled", 1)
		self.bar = frappe.get_doc({"doctype": "Rate Plan", "property": self.property,
			"rate_plan_name": "Best Available Rate", "code": "BAR",
			"is_default": 1}).insert(ignore_permissions=True).name
		for m in frappe.get_all("Channel Room Mapping", filters={"connection": self.conn.name,
				"external_rate_id": ("like", "%-bar")}, pluck="name"):
			doc = frappe.get_doc("Channel Room Mapping", m)
			doc.rate_plan = self.bar
			doc.save(ignore_permissions=True)


class ChannexRestrictionsTestCase(_AriWorkerMixin, _BarPlanFixture, IntegrationTestCase):
	def save(self, *rows) -> list[str]:
		from kamra.channel_restrictions import save_restrictions
		return save_restrictions(self.property, frappe.as_json(list(rows)))["saved"]

	def row(self, room_type, rate_plan, start: int, end: int | None = None, **rules) -> dict:
		return {"room_type": room_type, "rate_plan": rate_plan, "date_from": day(start),
		        "date_to": day(start if end is None else end), **rules}

	def posts(self) -> list[list[dict]]:
		return self.fake.posts("restrictions")

	def minimum_season(self, room_type, start: int, end: int, value, minimum: int):
		return frappe.get_doc({"doctype": "Season", "property": self.property,
			"season_name": f"Min {minimum} {start}-{end}", "room_type": room_type,
			"start_date": day(start), "end_date": day(end), "adjustment_type": "Absolute",
			"adjustment_value": value, "minimum_nights": minimum, "priority": 10}).insert(
			ignore_permissions=True)

	@staticmethod
	def strip(values: list[dict]) -> list[dict]:
		"""A call's values without property_id, sorted by plan and date."""
		out = [{k: v for k, v in x.items() if k != "property_id"} for x in values]
		return sorted(out, key=lambda x: (x["rate_plan_id"], x.get("date") or x["date_from"]))

	# ------------------------------------------------------------ each field
	def test_each_restriction_goes_to_its_own_channex_field_never_min_stay(self):
		"""Every type of restriction, one day, Twin BAR: the cell carries the
		six fields, and the call sends them by their Channex names."""
		self.save(self.row(self.twin, self.bar, 6, stop_sell=1, closed_to_arrival=1,
			closed_to_departure=1, min_stay_arrival=2, min_stay_through=3, max_stay=5))
		cells = self.ari.restriction_cells(self.conn, day(6), day(6))
		self.assertEqual({k: cells["rp-twin-bar"][day(6)][k] for k in RULE_FIELDS}, {
			"min_stay_arrival": 2, "min_stay_through": 3, "max_stay": 5,
			"closed_to_arrival": True, "closed_to_departure": True, "stop_sell": True})
		self.assertEqual(cells["rp-twin-bar"][day(6)]["rate"], self.ari._plan_night_rate(
			self.property, self.twin, self.bar, getdate(day(6))))
		self.assertEqual({k: cells["rp-twin-bb"][day(6)][k] for k in RULE_FIELDS}, {
			"min_stay_arrival": 1, "min_stay_through": 1, "max_stay": 0,
			"closed_to_arrival": False, "closed_to_departure": False, "stop_sell": False})
		self.flush()
		self.assertEqual(self.strip(self.posts()[0]), [{"rate_plan_id": "rp-twin-bar", "date": day(6),
			"min_stay_arrival": 2, "min_stay_through": 3, "max_stay": 5,
			"closed_to_arrival": True, "closed_to_departure": True, "stop_sell": True}])
		self.assertFalse(any("min_stay" in v for call in self.posts() for v in call))

	# ------------------------------------------- Kamra 2.8's Rate Restrictions
	def test_a_rate_restriction_reaches_channex_with_the_stay_rules_strictest_wins(self):
		"""Kamra 2.8's own Rate Restrictions (#530) refuse a direct booking, so
		they go out too, folded into the stay rules of the same day: the higher
		arrival minimum, the lower maximum, the flags OR-ed (HR-319). A day
		with only one of the two carries that one."""
		self.save(self.row(self.twin, self.bar, 6, 7, min_stay_arrival=2, max_stay=5,
			closed_to_departure=1))
		frappe.get_doc({"doctype": "Rate Restriction", "property": self.property,
			"restriction_name": "HR-319 " + frappe.generate_hash(length=6), "room_type": self.twin,
			"rate_plan": self.bar, "start_date": day(7), "end_date": day(8), "min_nights": 3,
			"max_nights": 4, "closed_to_arrival": 1}).insert(ignore_permissions=True)
		cells = self.ari.restriction_cells(self.conn, day(6), day(8))["rp-twin-bar"]
		rules = {n: {k: cells[day(n)][k] for k in RULE_FIELDS} for n in (6, 7, 8)}
		self.assertEqual(rules[6], {"min_stay_arrival": 2, "min_stay_through": 1, "max_stay": 5,
			"closed_to_arrival": False, "closed_to_departure": True, "stop_sell": False})
		self.assertEqual(rules[7], {"min_stay_arrival": 3, "min_stay_through": 1, "max_stay": 4,
			"closed_to_arrival": True, "closed_to_departure": True, "stop_sell": False})
		self.assertEqual(rules[8], {"min_stay_arrival": 3, "min_stay_through": 1, "max_stay": 4,
			"closed_to_arrival": True, "closed_to_departure": False, "stop_sell": False})
		# the price is still the night's: a restriction never zeroes the rate it sends
		self.assertEqual(cells[day(7)]["rate"], self.ari._plan_night_rate(
			self.property, self.twin, self.bar, getdate(day(7))))

	# ------------------------------------------------- certification 5 to 8
	def test_cert_05_min_stay_one_call(self):
		"""Case 5 · Min Stay: a minimum stay on three plans and days (Twin BAR
		3, Double BAR 2, Double BB 5), saved together from the screen -> 1
		POST restrictions with the three."""
		self.save(self.row(self.twin, self.bar, 5, min_stay_arrival=3),
		          self.row(self.double, self.bar, 9, min_stay_arrival=2),
		          self.row(self.double, self.bb, 14, min_stay_arrival=5))
		out = self.flush()
		self.assertEqual(len(self.posts()), 1)
		self.assertEqual(self.strip(self.posts()[0]), [
			{"rate_plan_id": "rp-double-bar", "date": day(9), "min_stay_arrival": 2},
			{"rate_plan_id": "rp-double-bb", "date": day(14), "min_stay_arrival": 5},
			{"rate_plan_id": "rp-twin-bar", "date": day(5), "min_stay_arrival": 3}])
		self.assertEqual(out["restrictions"]["sent"], 3)
		self.assertEqual(self.fake.posts("availability"), [])

	def test_cert_06_stop_sell_one_call(self):
		"""Case 6 · Stop Sell: three stop sells on three plans and days, in one
		save -> 1 call with stop_sell: true on each."""
		self.save(self.row(self.twin, self.bar, 4, stop_sell=1),
		          self.row(self.double, self.bar, 8, stop_sell=1),
		          self.row(self.double, self.bb, 12, stop_sell=1))
		self.flush()
		self.assertEqual(len(self.posts()), 1)
		self.assertEqual(self.strip(self.posts()[0]), [
			{"rate_plan_id": "rp-double-bar", "date": day(8), "stop_sell": True},
			{"rate_plan_id": "rp-double-bb", "date": day(12), "stop_sell": True},
			{"rate_plan_id": "rp-twin-bar", "date": day(4), "stop_sell": True}])

	def test_cert_07_multiple_restrictions_one_call(self):
		"""Case 7 · Multiple Restrictions: four range rows on four plans - Twin
		BAR CTA + max stay 4 + min stay 1, Double BAR CTD + min stay 6, Twin BB
		CTA + min stay 2, Double BB min stay 10 -> 1 call of ranges with those
		fields. A minimum of 1 is what Channex already holds, so it does not go
		again (only changes are sent)."""
		self.save(self.row(self.twin, self.bar, 20, 26, closed_to_arrival=1, max_stay=4, min_stay_arrival=1),
		          self.row(self.double, self.bar, 20, 26, closed_to_departure=1, min_stay_arrival=6),
		          self.row(self.twin, self.bb, 20, 26, closed_to_arrival=1, min_stay_arrival=2),
		          self.row(self.double, self.bb, 20, 26, min_stay_arrival=10))
		self.flush()
		self.assertEqual(len(self.posts()), 1)
		span = {"date_from": day(20), "date_to": day(26)}
		self.assertEqual(self.strip(self.posts()[0]), [
			{"rate_plan_id": "rp-double-bar", **span, "closed_to_departure": True, "min_stay_arrival": 6},
			{"rate_plan_id": "rp-double-bb", **span, "min_stay_arrival": 10},
			{"rate_plan_id": "rp-twin-bar", **span, "closed_to_arrival": True, "max_stay": 4},
			{"rate_plan_id": "rp-twin-bb", **span, "closed_to_arrival": True, "min_stay_arrival": 2}])

	def test_cert_08_half_year_one_call(self):
		"""Case 8 · Half-year: price and minimum stay over ~150 days (December
		to May in the guide) on Twin (Absolute 432, minimum 2) and Double
		(Absolute 342, minimum 3), two Seasons saved within 20 s with a 30 s
		batch window -> 1 call with the rate and min_stay_through in ranges,
		on the plans of each room type (a Season prices every plan of it)."""
		start, end = 10, 160
		self.baseline(end + 2)
		self.conn.db_set("ari_batch_seconds", 30)
		for rt, value, minimum in ((self.twin, 432, 2), (self.double, 342, 3)):
			self.minimum_season(rt, start, end, value, minimum)
			self.tick(10)
		self.tick(5)
		self.assertIn("waiting", self.flush()["restrictions"])
		self.assertEqual(self.posts(), [])
		self.tick(6)
		self.flush()
		self.assertEqual(len(self.posts()), 1)
		span = {"date_from": day(start), "date_to": day(end)}
		self.assertEqual(self.strip(self.posts()[0]), [
			{"rate_plan_id": "rp-double-bar", **span, "rate": "342.00", "min_stay_through": 3},
			{"rate_plan_id": "rp-double-bb", **span, "rate": "362.00", "min_stay_through": 3},
			{"rate_plan_id": "rp-twin-bar", **span, "rate": "432.00", "min_stay_through": 2},
			{"rate_plan_id": "rp-twin-bb", **span, "rate": "452.00", "min_stay_through": 2}])

	# ------------------------------------------------------------ coherence
	def test_the_booking_page_and_the_cells_agree_on_every_short_stay(self):
		"""30 days of mixed rules (a Season minimum, an arrival minimum, CTA,
		CTD, a maximum, a stop sell, a through minimum, a weekday rule and a
		BB-only stop sell that /book does not sell): for each arrival and 1 to
		5 nights, what search_stay accepts for Twin is what the Twin BAR cells
		(the plan /book sells) say."""
		from kamra.public_api import search_stay
		self.minimum_season(self.twin, 5, 7, 100, 2)
		weekday = getdate(day(3)).strftime("%a").lower()
		self.save(self.row(self.twin, "", 10, min_stay_arrival=3),
		          self.row(self.twin, self.bar, 12, closed_to_arrival=1),
		          self.row("", "", 15, closed_to_departure=1),
		          self.row(self.twin, "", 18, 19, max_stay=2),
		          self.row(self.twin, self.bar, 22, stop_sell=1),
		          self.row("", self.bar, 26, min_stay_through=4),
		          self.row(self.twin, "", 3, 32, closed_to_arrival=1, days_of_week=weekday),
		          self.row(self.twin, self.bb, 28, stop_sell=1))
		cells = self.ari.restriction_cells(self.conn, day(3), day(38))["rp-twin-bar"]

		def allowed(arrival: int, nights: int) -> bool:
			stay = [cells[day(arrival + i)] for i in range(nights)]
			first, out = stay[0], cells[day(arrival + nights)]
			return not (any(c["stop_sell"] for c in stay) or first["closed_to_arrival"]
			            or out["closed_to_departure"] or nights < first["min_stay_arrival"]
			            or nights < max(c["min_stay_through"] for c in stay)
			            or (first["max_stay"] and nights > first["max_stay"]))

		mismatches, refused = [], 0
		for arrival in range(3, 33):
			for nights in range(1, 6):
				rows = search_stay(self.property, day(arrival), day(arrival + nights), 2, 0)
				got = next(r for r in rows if r["room_type"] == self.twin)["available"]
				refused += not got
				if got != allowed(arrival, nights):
					mismatches.append((arrival, nights, got))
		self.assertEqual(mismatches, [])
		self.assertGreater(refused, 20)

	def test_removing_a_stop_sell_sends_false_for_those_days_only(self):
		from kamra.channel_restrictions import delete_restrictions
		names = self.save(self.row(self.twin, self.bar, 6, 7, stop_sell=1))
		self.flush()
		delete_restrictions(self.property, frappe.as_json(names))
		self.flush()
		self.assertEqual(len(self.posts()), 2)
		self.assertEqual(self.strip(self.posts()[1]), [
			{"rate_plan_id": "rp-twin-bar", "date_from": day(6), "date_to": day(7), "stop_sell": False}])

	# ------------------------------------------------------------- triggers
	def test_a_moved_restriction_queues_its_old_and_new_dates(self):
		name = self.save(self.row(self.twin, "", 5, 6, min_stay_arrival=2))[0]
		frappe.db.delete("Channel ARI Outbox", {"connection": self.conn.name})
		frappe.local.kamra_ari_queued = None
		self.save({"name": name, "date_from": day(12), "date_to": day(14)})
		row = self.rows()[-1]
		self.assertEqual((str(row.date_from), str(row.date_to), row.kinds),
		                 (day(5), day(14), "restrictions"))
		self.assertIn(name, row.reason)

	def test_the_property_floor_queues_the_horizon_only_when_it_changes(self):
		prop = frappe.get_doc("Property", self.property)
		prop.save(ignore_permissions=True)
		self.assertEqual(self.rows(), [])
		prop.minimum_nights = 2
		prop.save(ignore_permissions=True)
		row = self.rows()[-1]
		self.assertEqual((str(row.date_from), str(row.date_to), row.kinds),
		                 (day(0), day(self.ari.ARI_HORIZON_DAYS - 1), "restrictions"))
		self.flush()
		sent = self.strip(self.posts()[0])
		self.assertEqual({v.get("min_stay_through") for v in sent}, {2})
		self.assertEqual({v["rate_plan_id"] for v in sent},
		                 {"rp-twin-bar", "rp-twin-bb", "rp-double-bar", "rp-double-bb"})

	def test_what_was_sent_before_the_new_fields_gets_them_once(self):
		"""After the deploy, Channel ARI State knows only rate and stop_sell:
		the next pass sends the five new fields for the whole window, once."""
		for ext in ("rp-twin-bar", "rp-twin-bb", "rp-double-bar", "rp-double-bb"):
			name = f"{self.conn.name}|restrictions|{ext}"
			old = {d: {k: v for k, v in vals.items() if k in ("rate", "stop_sell")}
			       for d, vals in self.sent("restrictions", ext).items()}
			frappe.db.set_value("Channel ARI State", name, "sent_values", frappe.as_json(old))
		self.ari.enqueue_ari_change(self.property, day(0), day(9), kinds="restrictions", reason="deploy")
		self.flush()
		self.assertEqual(len(self.posts()), 1)
		self.assertEqual(self.strip(self.posts()[0])[0], {"rate_plan_id": "rp-double-bar",
			"date_from": day(0), "date_to": day(9), "min_stay_arrival": 1, "min_stay_through": 1,
			"max_stay": 0, "closed_to_arrival": False, "closed_to_departure": False})
		self.ari.enqueue_ari_change(self.property, day(0), day(9), kinds="restrictions", reason="again")
		self.flush()
		self.assertEqual(len(self.posts()), 1)
