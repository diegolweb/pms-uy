# Copyright (c) 2026, Hotel Roosevelt and contributors
# For license information, please see license.txt

"""The indicators' rules (HR-379), without a Frappe site:
PYTHONPATH=vendor/kamra python3 -m unittest kamra.tests.test_indicator_math -v"""

import unittest
from datetime import date, datetime

from kamra import indicator_math as im

JAN, FEB, MAR = date(2027, 1, 1), date(2027, 2, 1), date(2027, 3, 1)


def res(name="R1", *, guest=None, status="Checked Out", created=date(2026, 12, 1),
        ci=date(2027, 1, 10), co=date(2027, 1, 12), base=200.0, commission=0.0,
        source="Website", channel=None, travel_agent=None, note=None, market=None):
	return im.Res(name=name, guest=guest, status=status,
	              creation=datetime.combine(created, datetime.min.time()),
	              check_in=ci, check_out=co, base=base, commission=commission, source=source,
	              channel=channel, travel_agent=travel_agent, cancellation_note=note, market=market)


def by_key(table):
	return {c["key"]: c for c in table["channels"]}


class TestIndicatorMath(unittest.TestCase):
	def test_r2_nights_and_revenue_split_by_month_and_day_use(self):
		stay = res(ci=date(2027, 1, 30), co=date(2027, 2, 3), base=400.0)
		day_use = res("R2", ci=date(2027, 1, 20), co=date(2027, 1, 20), base=50.0, source="Phone")
		jan = by_key(im.channel_table([stay, day_use], JAN, FEB, {}, None))
		feb = by_key(im.channel_table([stay, day_use], FEB, MAR, {}, None))
		self.assertEqual((jan["Website"]["nights"], jan["Website"]["revenue"]), (2, 200))
		self.assertEqual((feb["Website"]["nights"], feb["Website"]["revenue"]), (2, 200))
		self.assertEqual((jan["Phone"]["nights"], jan["Phone"]["revenue"]), (0, 50))
		self.assertNotIn("Phone", feb)
		# the base: the website's carry all three figures; platforms and manual ones, before = 0 and tax = 0
		self.assertEqual(im.base_of(100.0, 122.0, 22.0), 100.0)
		self.assertEqual(im.base_of(0, 150.0, 0), 150.0)

	def test_r4_commission_stored_pct_missing_and_agent(self):
		stored = res("A", source="OTA", channel="Booking.com", commission=30.0)
		by_pct = res("B", source="OTA", channel="Booking.com")
		missing = res("C", source="OTA", channel="Expedia")
		agent = res("D", source="Phone", travel_agent="TA-1", commission=20.0)
		pcts = {"Booking.com": 15.0, "Expedia": 0.0}
		self.assertEqual(im.commission_of(stored, pcts), (30.0, "stored"))
		self.assertEqual(im.commission_of(by_pct, pcts), (30.0, "pct"))
		self.assertEqual(im.commission_of(missing, pcts), (0.0, "missing"))
		self.assertEqual(im.commission_of(agent, pcts), (20.0, "stored"))
		t = im.channel_table([stored, by_pct, missing, agent], JAN, FEB, pcts, None)
		self.assertEqual(t["missing_commission"], ["Expedia"])
		exp = by_key(t)["Expedia"]
		self.assertIsNone(exp["distribution_cost_pct"])
		self.assertIsNone(exp["net_adr"])
		self.assertIsNotNone(by_key(t)["Booking.com"]["net_adr"])
		self.assertIsNone(t["totals"]["distribution_cost_pct"])
		self.assertIsNone(t["totals"]["net_adr"])

	def test_r5_distribution_cost_and_net_rate_as_the_mockup(self):
		# one stay per channel with the mockup's nights: 164 and 78
		booking = res("B", source="OTA", channel="Booking.com", ci=JAN,
		              co=date.fromordinal(JAN.toordinal() + 164), base=24272.0, commission=3641.0)
		web = res("W", ci=JAN, co=date.fromordinal(JAN.toordinal() + 78), base=11388.0)
		far = date(2027, 12, 31)
		inputs = {"ads": [{"name": "Booking · destacado", "channel": "Booking.com", "spend": 410, "sales": 3280},
		                  {"name": "Google · verano", "channel": "Website", "spend": 300, "sales": 2940}],
		          "payment_costs": [{"channel": "Website", "amount": 228}]}
		t = by_key(im.channel_table([booking, web], JAN, far, {}, inputs))
		self.assertEqual((t["Booking.com"]["distribution_cost_pct"], t["Booking.com"]["net_adr"]), (16.7, 123))
		self.assertEqual((t["Website"]["distribution_cost_pct"], t["Website"]["net_adr"]), (4.6, 139))

	def test_r6_direct_share_leaves_out_ota_and_agents(self):
		rows = [res("O", source="OTA", channel="Booking.com", base=200.0),
		        res("A", source="Phone", travel_agent="TA-1", base=200.0),
		        res("W", source="Website", base=100.0),
		        res("P", source="Phone", base=100.0),
		        res("K", source="Walk-in", base=200.0)]
		t = im.channel_table(rows, JAN, FEB, {"Booking.com": 10.0}, None)
		self.assertEqual(t["direct_nights_pct"], 60.0)  # 6 of 10 nights
		self.assertEqual(t["direct_revenue_pct"], 50.0)  # 400 of 800

	def test_r7_cancellations_and_no_shows_without_the_never_sold(self):
		rows = [res("H", status="Cancelled", note=im.HOLD_EXPIRY_NOTE),
		        res("D", status="Cancelled", note="Request declined: full"),
		        res("C", status="Cancelled", note="guest changed plans"),
		        res("S1"), res("S2"), res("N", status="No Show"),
		        res("O", status="Cancelled", source="OTA", channel="Booking.com"),
		        res("O2", source="OTA", channel="Booking.com", commission=10.0)]
		t = im.channel_table(rows, JAN, FEB, {}, None)
		web = by_key(t)["Website"]
		self.assertEqual(web["arrivals"], 4)
		self.assertEqual((web["cancelled_pct"], web["no_show_pct"]), (25.0, 25.0))
		self.assertEqual(by_key(t)["Booking.com"]["cancelled_pct"], 50.0)
		self.assertEqual((t["totals"]["arrivals"], t["totals"]["cancelled_pct"]), (6, 33.3))

	def test_r8_lead_time_by_market_unknown_last(self):
		rows = [res("A1", market="AR", created=date(2027, 1, 1), ci=date(2027, 1, 11)),
		        res("A2", market="AR", created=date(2027, 1, 1), ci=date(2027, 1, 12)),
		        res("U", market="UY", created=date(2027, 1, 9), ci=date(2027, 1, 10)),
		        res("X", market=None, created=date(2026, 12, 31), ci=date(2027, 1, 5)),
		        res("Y", market=None, created=date(2026, 12, 31), ci=date(2027, 1, 5)),
		        res("Y2", market=None, created=date(2026, 12, 31), ci=date(2027, 1, 5)),
		        res("C", market="BR", status="Cancelled", ci=date(2027, 1, 5))]
		out, total = im.lead_times(rows, JAN, FEB)
		self.assertEqual(out, [{"market": "AR", "reservations": 2, "avg_days": 11},  # 10.5 → 11
		                       {"market": "UY", "reservations": 1, "avg_days": 1},
		                       {"market": None, "reservations": 3, "avg_days": 5}])
		self.assertEqual(total, {"reservations": 6, "avg_days": 6})  # 37 / 6

	def test_r9_returning_guests_need_an_earlier_stay(self):
		rows = [res("NOW1", guest="G1", ci=date(2027, 1, 10)),
		        res("NOW2", guest="G2", ci=date(2027, 1, 10)),
		        res("FIRST", guest="G3", ci=date(2027, 1, 3), co=date(2027, 1, 5)),
		        res("AGAIN", guest="G3", ci=date(2027, 1, 20), co=date(2027, 1, 22))]
		history = [("OLD1", "G1", "Checked Out", date(2026, 6, 1)),
		           ("OLD2", "G2", "Cancelled", date(2026, 6, 1)),
		           ("FIRST", "G3", "Checked Out", date(2027, 1, 3)),
		           ("AGAIN", "G3", "Checked Out", date(2027, 1, 20))]
		returning = im.returning_names(rows, JAN, FEB, history)
		self.assertEqual(returning, {"NOW1", "AGAIN"})
		self.assertEqual(im.repeat_share(rows, JAN, FEB, returning),
		                 {"repeat": 2, "reservations": 4, "repeat_pct": 50.0})

	def test_r10_booked_ahead_is_a_sum_and_no_capacity_gives_0(self):
		forecast = [{"booked": 10, "held": 0, "available": 0}] + \
		           [{"booked": 0, "held": 0, "available": 1}] * 29 + \
		           [{"booked": 0, "held": 0, "available": 0}] * 60
		got = im.on_books(forecast, date(2027, 1, 18))
		# the mean of the daily %s would say 3.3 %; booked over sellable is 10 of 39
		self.assertEqual(got[0], {"days": 30, "until": "2027-02-16", "occupancy_pct": 25.6})
		self.assertEqual(got[2]["until"], "2027-04-17")
		empty = im.on_books([{"booked": 0, "held": 0, "available": 0}] * 90, date(2027, 1, 18))
		self.assertEqual([w["occupancy_pct"] for w in empty], [0, 0, 0])

	def test_r11_r12_outside_kamra_and_its_checks(self):
		self.assertIsNone(im.outside({"booking_page_views": 0}, 61)["booking_conversion_pct"])
		got = im.outside(im.clean_inputs({"booking_page_views": 4120, "ads": [
			{"name": "Google · verano", "channel": "Website", "spend": 0, "sales": 50}]}, set()), 61)
		self.assertEqual(got["booking_conversion_pct"], 1.5)
		self.assertIsNone(got["ads"][0]["roas"])
		for bad in ({"reviews": [{"platform": "Google", "score": 5.5, "count": 3}]},
		            {"booking_page_views": -1},
		            {"ads": [{"name": "X", "channel": "Expedia", "spend": 1, "sales": 1}]}):
			with self.assertRaises(ValueError):
				im.clean_inputs(bad, {"Booking.com"})


if __name__ == "__main__":
	unittest.main()
