"""HR-378: booking pace (pickup). Pure rules, no Frappe: the ones that, if
wrong, show a false figure (sold at a moment, moved dates, nights in a
period, target / pace / status, channels, and the overview)."""

import json
import unittest
from datetime import date, datetime, timedelta

from kamra.pickup_pace import HOLD_EXPIRY_NOTE, Change, Stay, nights_in, pace, periods, state_at

NOW = datetime(2026, 11, 27, 20, 0)
TODAY = date(2026, 11, 27)
WEEK_A = date(2026, 12, 28)  # a Monday: 28 Dec - 4 Jan, four nights in December and three in January
WEEK_B = date(2027, 1, 4)
ALL_YEAR = {f"{y}-{m:02d}": 50.0 for y in (2026, 2027) for m in range(1, 13)}


def ago(days: float) -> datetime:
	return NOW - timedelta(days=days)


def stay(name, check_in, nights, created, status="Confirmed", cancelled_on=None, note=None,
         source="Website", channel=None) -> Stay:
	return Stay(name, status, created, cancelled_on, note, check_in, check_in + timedelta(days=nights),
	            source, channel)


def change(at, *rows) -> Change:
	return Change(at, json.dumps({"changed": [list(r) for r in rows], "added": [], "removed": []}))


def report(stays, changes=None, rooms=10, targets=ALL_YEAR, start=WEEK_A, grain="week"):
	return pace(stays, changes or {}, now=NOW, today=TODAY, start=start, grain=grain, rooms=rooms,
	            targets=targets)


class TestPickupPace(unittest.TestCase):
	def test_sold_at_a_moment(self):
		dates = (WEEK_A, WEEK_A + timedelta(days=2))
		confirmed = stay("R1", WEEK_A, 2, ago(10))
		self.assertIsNone(state_at(confirmed, [], ago(11)))
		self.assertEqual(state_at(confirmed, [], ago(9)), dates)

		paid_later = stay("R2", WEEK_A, 2, ago(5))
		log = [change(ago(3), ("status", "Pending Payment", "Confirmed"))]
		self.assertIsNone(state_at(paid_later, log, ago(4)))
		self.assertEqual(state_at(paid_later, log, ago(2)), dates)

		expired = stay("R3", WEEK_A, 2, ago(5), status="Cancelled", cancelled_on=ago(4))
		log = [change(ago(4), ("status", "Pending Payment", "Cancelled"))]
		self.assertIsNone(state_at(expired, log, ago(4.5)))
		self.assertIsNone(state_at(expired, log, ago(1)))

		cancelled = stay("R4", WEEK_A, 2, ago(10), status="Cancelled", cancelled_on=ago(2))
		log = [change(ago(2), ("status", "Confirmed", "Cancelled"))]
		self.assertEqual(state_at(cancelled, log, ago(3)), dates)
		self.assertIsNone(state_at(cancelled, log, ago(1)))

		# no status row in the log: fall back on cancelled_on and the note
		hold = stay("R5", WEEK_A, 2, ago(10), status="Cancelled", cancelled_on=ago(2), note=HOLD_EXPIRY_NOTE)
		self.assertIsNone(state_at(hold, [], ago(3)))
		plain = stay("R6", WEEK_A, 2, ago(10), status="Cancelled", cancelled_on=ago(2), note="Guest called")
		self.assertEqual(state_at(plain, [], ago(3)), dates)
		self.assertIsNone(state_at(plain, [], ago(1)))

	def test_moved_dates_move_the_pickup(self):
		moved = stay("R1", WEEK_B, 3, ago(20))
		log = {"R1": [change(ago(3), ("check_in_date", "2026-12-28", "2027-01-04"),
		                     ("check_out_date", "2026-12-31", "2027-01-07"))]}
		a, b = report([moved], log)["periods"][:2]
		self.assertEqual((a["on_books"], a["pickup_7"], a["pickup_30"]), (0, -3, 0))
		self.assertEqual((b["on_books"], b["pickup_7"], b["pickup_30"]), (3, 3, 3))

	def test_nights_in_a_period(self):
		self.assertEqual(nights_in(date(2027, 1, 2), date(2027, 1, 6), WEEK_A, WEEK_B), 2)
		self.assertEqual(nights_in(date(2027, 1, 2), date(2027, 1, 6), WEEK_B, WEEK_B + timedelta(days=7)), 2)
		self.assertEqual(nights_in(WEEK_B, WEEK_B, WEEK_B, WEEK_B + timedelta(days=7)), 0)
		self.assertEqual(periods(date(2026, 12, 30), "week")[0], (date(2026, 12, 30), WEEK_B))

	def test_target_pace_and_status(self):
		straddle = report([], rooms=25, targets={"2026-12": 85.0, "2027-01": 75.0})["periods"]
		self.assertAlmostEqual(straddle[0]["target_nights"], 25 * (4 * 0.85 + 3 * 0.75), delta=0.1)
		self.assertEqual(straddle[0]["status"], "behind")
		self.assertEqual(straddle[5]["status"], "no_target")  # 1 Feb: no February target

		weeks = [WEEK_A + timedelta(weeks=i) for i in range(5)]
		stays = [stay(f"C{i}", weeks[0], 7, ago(20)) for i in range(5)]  # 35 = the target (10 rooms x 50 %)
		stays += [stay("P0", weeks[1], 7, ago(2))]  # 7 in the last 7 days
		stays += [stay("B0", weeks[2], 5, ago(20))]  # 5, nothing lately
		stays += [stay(f"X{i}", weeks[3], 7, ago(2)) for i in range(5)]  # 35 lately: capped
		stays += [stay("N0", weeks[4], 7, ago(20)),
		          stay("N1", weeks[4], 7, ago(20), status="Cancelled", cancelled_on=ago(2))]
		log = {"N1": [change(ago(2), ("status", "Confirmed", "Cancelled"))]}
		rows = report(stays, log)["periods"]
		self.assertEqual([r["status"] for r in rows[:4]], ["covered", "on_pace", "behind", "covered"])
		self.assertEqual(rows[1]["projected_nights"], round(7 + 7 * (weeks[1] - TODAY).days / 7, 1))
		self.assertEqual(rows[3]["projected_nights"], 70)  # 35 + 35 a week would pass the 70-night capacity
		self.assertEqual((rows[4]["pickup_7"], rows[4]["projected_nights"]), (-7, 7))

	def test_channels(self):
		stays = [stay("O1", WEEK_A, 3, ago(2), source="OTA", channel="Booking.com"),
		         stay("O2", WEEK_A, 2, ago(20), source="OTA"),
		         stay("W1", WEEK_B, 4, ago(10), source="Website")]
		out = report(stays)
		self.assertEqual([(c["label"], c["kind"], c["on_books"]) for c in out["channels"]],
		                 [("Website", "source", 4), ("Booking.com", "channel", 3), ("OTA", "channel", 2)])
		for k in ("on_books", "pickup_7", "pickup_30"):
			self.assertEqual(out["totals"][k], sum(c[k] for c in out["channels"]))
			self.assertEqual(out["totals"][k], sum(p[k] for p in out["periods"]))

	def test_overview_is_the_aggregate_of_its_rows(self):
		stays = [stay("S1", WEEK_A, 4, ago(20)),
		         stay("S2", WEEK_A, 3, ago(20), status="Cancelled", cancelled_on=ago(2)),
		         stay("S3", WEEK_B, 3, ago(20)),
		         stay("S4", WEEK_B + timedelta(days=7), 7, ago(1))]
		log = {"S2": [change(ago(2), ("status", "Confirmed", "Cancelled"))],
		       "S3": [change(ago(3), ("check_in_date", "2026-12-31", "2027-01-04"),
		                     ("check_out_date", "2027-01-03", "2027-01-07"))]}
		out = report(stays, log)
		rows, s = out["periods"], out["summary"]
		self.assertEqual((s["on_books"], s["pickup_7"], s["pickup_30"]), (14, 4, 14))  # 7 in, 3 cancelled
		self.assertEqual(s["capacity"], sum(r["capacity"] for r in rows))
		self.assertEqual(s["occupancy_pct"], round(100 * 14 / s["capacity"], 1))
		self.assertEqual(s["projected_nights"], round(sum(r["projected_nights"] for r in rows), 1))
		self.assertEqual(s["target_nights"], round(sum(r["target_nights"] for r in rows), 1))
		self.assertEqual(s["target_pct"], round(100 * s["target_nights"] / s["capacity"], 1))
		self.assertEqual(s["status"], {k: sum(1 for r in rows if r["status"] == k)
		                               for k in ("behind", "on_pace", "covered")})
		self.assertEqual(report(stays, log, targets={"2026-12": 85.0})["summary"]["target_nights"], None)


if __name__ == "__main__":
	unittest.main()
