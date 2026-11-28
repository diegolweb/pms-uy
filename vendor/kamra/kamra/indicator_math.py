# Copyright (c) 2026, Hotel Roosevelt and contributors
# For license information, please see license.txt

"""The marketing plan's indicators (HR-379): the rules that turn reservations
into figures, with no Frappe and no phonenumbers, so they test on their own.

kamra.indicators reads the rows and hands them here; every number the
Indicators screen shows that is not a straight read comes out of this file.
Percentages carry one decimal, money and rates are whole; None means «—»."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

# Sold, for nights, revenue, mix, direct share and costs (as `contribution`).
STAYED = frozenset({"Confirmed", "Checked In", "Checked Out"})
STAYED_BEFORE = frozenset({"Checked In", "Checked Out"})
# Never sold: copied, not imported, to stay free of Frappe
# (reservation_state.HOLD_EXPIRY_NOTE; booking_requests' declined note).
HOLD_EXPIRY_NOTE = "Hold / payment window expired"
DECLINED_PREFIX = "Request declined:"
# Review platforms and their rating scale.
PLATFORMS = (("Booking.com", 10), ("Google", 5), ("Tripadvisor", 5), ("Expedia", 10),
             ("Despegar", 10), ("Airbnb", 5))
# The channel an ad or a payment cost may name even with no booking in it.
WEBSITE = "Website"


@dataclass(frozen=True)
class Res:
	name: str
	guest: str | None
	status: str
	creation: datetime
	check_in: date
	check_out: date
	base: float  # pre-tax price of the whole stay
	commission: float  # commission_amount as stored
	source: str | None
	channel: str | None
	travel_agent: str | None
	cancellation_note: str | None
	market: str | None  # ISO-2 region of the guest's phone, or None


# --- small helpers -----------------------------------------------------------

def _round(x: float, nd: int = 0):
	"""Half up, as a person rounds (Python's round() is half to even)."""
	q = Decimal(1).scaleb(-nd)
	v = Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP)
	return int(v) if nd == 0 else float(v)


def _pct(part: float, whole: float):
	return _round(100 * part / whole, 1) if whole else None


def _day(d) -> date:
	return d.date() if isinstance(d, datetime) else d


def base_of(before, after, tax) -> float:
	"""The stay's money before tax: amount_before_tax when the booking has it
	(the website's carry all three figures), else after tax less the tax. The
	platforms' and manual bookings arrive with before = 0 and tax = 0, so
	theirs is the amount as it came."""
	return float(before or 0) or float(after or 0) - float(tax or 0)


def never_sold(r: Res) -> bool:
	"""An expired hold or a declined request: it never was a sale."""
	note = r.cancellation_note or ""
	return note == HOLD_EXPIRY_NOTE or note.startswith(DECLINED_PREFIX)


def nights_in(ci: date, co: date, start: date, end: date) -> int:
	"""Nights of the stay ci..co inside [start, end) (end exclusive)."""
	return max(0, (min(co, end) - max(ci, start)).days)


def channel_of(r: Res) -> tuple[str, str]:
	"""(key, kind): an OTA by its channel, anything else by its source."""
	if r.source == "OTA":
		return (r.channel or "OTA", "channel")
	return (r.source or "Manual", "source")


def commission_of(r: Res, pcts: dict[str, float]) -> tuple[float, str]:
	"""The whole stay's commission and where it came from: what the platform
	sent (stored), the OTA's current % (pct), or none because the % is not
	set (missing). A booking that came in without a % kept 0, so the current
	% fixes past months too."""
	if (r.commission or 0) > 0:
		return (float(r.commission), "stored")
	if r.source == "OTA":
		pct = float(pcts.get(r.channel or "", 0) or 0)
		if pct > 0:
			return (float(r.base or 0) * pct / 100, "pct")
		return (0.0, "missing")
	return (float(r.commission or 0), "stored")


def _share(r: Res, start: date, end: date) -> float | None:
	"""The part of the stay's money that falls in [start, end): by nights, or
	whole in the arrival month for a day use. None when nothing falls in."""
	nights = (r.check_out - r.check_in).days
	if nights <= 0:
		return 1.0 if start <= r.check_in < end else None
	n = nights_in(r.check_in, r.check_out, start, end)
	return n / nights if n else None


def _is_direct(r: Res) -> bool:
	return r.source != "OTA" and not r.travel_agent


# --- the month by channel ----------------------------------------------------

def channel_table(rows: list[Res], start: date, end: date, pcts: dict[str, float],
                  inputs: dict | None) -> dict:
	"""Nights, revenue, mix, commission, distribution cost, net rate and
	cancellations / no-shows per channel for the stays in [start, end)."""
	inputs = inputs or {}
	pay = {}
	for p in inputs.get("payment_costs") or []:
		pay[p["channel"]] = pay.get(p["channel"], 0.0) + float(p.get("amount") or 0)
	ads = {}
	for a in inputs.get("ads") or []:
		ads[a["channel"]] = ads.get(a["channel"], 0.0) + float(a.get("spend") or 0)

	acc: dict[str, dict] = {}

	def row(r: Res) -> dict:
		key, kind = channel_of(r)
		return acc.setdefault(key, {"key": key, "kind": kind, "nights": 0, "revenue": 0.0,
		                            "commission": 0.0, "arrivals": 0, "cancelled": 0,
		                            "no_show": 0, "missing": False})

	direct_n = direct_rev = 0.0
	for r in rows:
		if r.status not in STAYED:
			continue
		share = _share(r, start, end)
		if share is None:
			continue
		c = row(r)
		n = nights_in(r.check_in, r.check_out, start, end)
		rev = float(r.base or 0) * share
		com, how = commission_of(r, pcts)
		c["nights"] += n
		c["revenue"] += rev
		c["commission"] += com * share
		c["missing"] = c["missing"] or how == "missing"
		if _is_direct(r):
			direct_n += n
			direct_rev += rev

	for r in rows:
		if not (start <= r.check_in < end) or never_sold(r):
			continue
		if r.status not in STAYED and r.status not in ("No Show", "Cancelled"):
			continue
		c = row(r)
		c["arrivals"] += 1
		c["cancelled"] += r.status == "Cancelled"
		c["no_show"] += r.status == "No Show"

	tot_n = sum(c["nights"] for c in acc.values())
	tot_rev = sum(c["revenue"] for c in acc.values())
	missing = sorted(k for k, c in acc.items() if c["missing"])
	channels = []
	for c in acc.values():
		p, a = pay.get(c["key"], 0.0), ads.get(c["key"], 0.0)
		cost = c["commission"] + p + a
		known = not c["missing"] and c["revenue"] > 0 and c["nights"] > 0
		channels.append({
			"key": c["key"], "kind": c["kind"],
			"nights": c["nights"], "nights_pct": _pct(c["nights"], tot_n),
			"revenue": _round(c["revenue"]), "revenue_pct": _pct(c["revenue"], tot_rev),
			"adr": _round(c["revenue"] / c["nights"]) if c["nights"] else None,
			"commission": _round(c["commission"]), "payment_cost": _round(p), "ads_spend": _round(a),
			"distribution_cost_pct": _pct(cost, c["revenue"]) if known else None,
			"net_adr": _round((c["revenue"] - cost) / c["nights"]) if known else None,
			"arrivals": c["arrivals"],
			"cancelled_pct": _pct(c["cancelled"], c["arrivals"]),
			"no_show_pct": _pct(c["no_show"], c["arrivals"]),
		})
	channels.sort(key=lambda c: (-c["nights"], -c["revenue"], c["key"]))

	# the total carries every cost entered, also one on a channel with no stay
	tot_com = sum(c["commission"] for c in acc.values())
	tot_pay, tot_ads = sum(pay.values()), sum(ads.values())
	tot_cost = tot_com + tot_pay + tot_ads
	known = not missing and tot_rev > 0 and tot_n > 0
	arr = sum(c["arrivals"] for c in acc.values())
	totals = {
		"nights": tot_n, "revenue": _round(tot_rev),
		"adr": _round(tot_rev / tot_n) if tot_n else None,
		"commission": _round(tot_com), "payment_cost": _round(tot_pay), "ads_spend": _round(tot_ads),
		"distribution_cost_pct": _pct(tot_cost, tot_rev) if known else None,
		"net_adr": _round((tot_rev - tot_cost) / tot_n) if known else None,
		"arrivals": arr,
		"cancelled_pct": _pct(sum(c["cancelled"] for c in acc.values()), arr),
		"no_show_pct": _pct(sum(c["no_show"] for c in acc.values()), arr),
	}
	return {"channels": channels, "totals": totals, "missing_commission": missing,
	        "direct_nights_pct": _pct(direct_n, tot_n),
	        "direct_revenue_pct": _pct(direct_rev, tot_rev)}


# --- arrivals in the month: lead time and returning guests -------------------

def _arrived(rows: list[Res], start: date, end: date) -> list[Res]:
	return [r for r in rows if start <= r.check_in < end
	        and (r.status in STAYED or r.status == "No Show")]


def lead_times(rows: list[Res], start: date, end: date) -> tuple[list[dict], dict]:
	"""Average days between booking and arrival, per market (most bookings
	first, the unknown market last) and for all."""
	by: dict[str | None, list[int]] = {}
	every = []
	for r in _arrived(rows, start, end):
		d = max(0, (r.check_in - _day(r.creation)).days)
		by.setdefault(r.market, []).append(d)
		every.append(d)
	out = [{"market": m, "reservations": len(v), "avg_days": _round(sum(v) / len(v))}
	       for m, v in by.items()]
	out.sort(key=lambda x: (x["market"] is None, -x["reservations"], x["market"] or ""))
	total = {"reservations": len(every),
	         "avg_days": _round(sum(every) / len(every)) if every else None}
	return out, total


def returning_names(rows: list[Res], start: date, end: date,
                    history: list[tuple[str, str, str, date]]) -> set[str]:
	"""The arrivals of the month whose guest had another stay (checked in or
	out) that arrived earlier. history = (name, guest, status, check_in) of
	those guests' bookings."""
	out = set()
	for r in _arrived(rows, start, end):
		if not r.guest:
			continue
		if any(g == r.guest and n != r.name and s in STAYED_BEFORE and _day(ci) < r.check_in
		       for n, g, s, ci in history):
			out.add(r.name)
	return out


def repeat_share(rows: list[Res], start: date, end: date, returning: set[str]) -> dict:
	arrived = _arrived(rows, start, end)
	rep = sum(1 for r in arrived if r.name in returning)
	return {"repeat": rep, "reservations": len(arrived), "repeat_pct": _pct(rep, len(arrived))}


# --- what is booked ahead ----------------------------------------------------

def on_books(forecast: list[dict], today: date, windows=(30, 60, 90)) -> list[dict]:
	"""Booked over sellable for the next N nights, summed (not an average of
	daily %s), from dashboards.occupancy_forecast."""
	out = []
	for n in windows:
		part = forecast[:n]
		booked = sum(int(d.get("booked") or 0) for d in part)
		cap = sum(int(d.get("booked") or 0) + int(d.get("held") or 0)
		          + int(d.get("available") or 0) for d in part)
		out.append({"days": n, "until": str(today + timedelta(days=n - 1)),
		            "occupancy_pct": _round(100 * booked / cap, 1) if cap else 0})
	return out


# --- outside Kamra -----------------------------------------------------------

def booking_reservations(rows: list[Res], month_start: date, month_next: date) -> int:
	"""Bookings Booking.com brought, by the day they were made, in the calendar
	month (cancelled later still count: the page converted)."""
	return sum(1 for r in rows if r.source == "OTA" and r.channel == "Booking.com"
	           and month_start <= _day(r.creation) < month_next and not never_sold(r))


def _num(v, what: str, *, integer=False):
	"""A number >= 0 (or None when empty) from a form value."""
	if v is None or (isinstance(v, str) and not v.strip()):
		return None
	if isinstance(v, bool):
		raise ValueError(f"{what}: not a number")
	if isinstance(v, str):
		try:
			v = float(v.strip().replace(",", "."))
		except ValueError:
			raise ValueError(f"{what}: not a number") from None
	if not isinstance(v, int | float):
		raise ValueError(f"{what}: not a number")
	v = float(v)
	if v != v or v in (float("inf"), float("-inf")):
		raise ValueError(f"{what}: not a number")
	if v < 0:
		raise ValueError(f"{what}: cannot be negative")
	if integer:
		if not v.is_integer():
			raise ValueError(f"{what}: must be a whole number")
		return int(v)
	return v


def clean_inputs(raw: dict, channels: set[str]) -> dict:
	"""The month's figures from outside Kamra, checked: numbers >= 0, a rating
	within its platform's scale, whole counts, known platforms, and channels
	that are the month's or Website. ValueError says what is wrong."""
	if not isinstance(raw, dict):
		raise ValueError("inputs: expected an object")
	allowed = set(channels) | {WEBSITE}
	scale = dict(PLATFORMS)
	out = {"booking_page_views": _num(raw.get("booking_page_views"), "Page views", integer=True),
	       "reviews": [], "ads": [], "payment_costs": []}
	seen = set()
	for rv in raw.get("reviews") or []:
		p = (rv or {}).get("platform")
		if p not in scale:
			raise ValueError(f"Unknown platform: {p}")
		if p in seen:
			raise ValueError(f"{p}: entered twice")
		seen.add(p)
		score = _num(rv.get("score"), f"{p} rating")
		count = _num(rv.get("count"), f"{p} number of reviews", integer=True)
		if score is not None and score > scale[p]:
			raise ValueError(f"{p} rating: out of {scale[p]}")
		if score is not None or count is not None:
			out["reviews"].append({"platform": p, "score": score, "count": count})
	for ad in raw.get("ads") or []:
		ad = ad or {}
		name = str(ad.get("name") or "").strip()
		spend = _num(ad.get("spend"), f"{name or 'Ad'} spend")
		sales = _num(ad.get("sales"), f"{name or 'Ad'} sales")
		if not name and spend is None and sales is None:
			continue
		if not name:
			raise ValueError("Ad: the name is missing")
		if ad.get("channel") not in allowed:
			raise ValueError(f"{name}: unknown channel {ad.get('channel')}")
		out["ads"].append({"name": name[:140], "channel": ad["channel"],
		                   "spend": spend or 0.0, "sales": sales or 0.0})
	paid = set()
	for pc in raw.get("payment_costs") or []:
		pc = pc or {}
		ch = pc.get("channel")
		if ch not in allowed:
			raise ValueError(f"Payment cost: unknown channel {ch}")
		if ch in paid:
			raise ValueError(f"Payment cost of {ch}: entered twice")
		paid.add(ch)
		amount = _num(pc.get("amount"), f"Payment cost of {ch}")
		if amount is not None:
			out["payment_costs"].append({"channel": ch, "amount": amount})
	return out


def outside(inputs: dict | None, booking_reservations: int) -> dict:
	"""Booking views and conversion, reviews and ads ROAS: what was entered
	for the month plus the Booking.com bookings Kamra counts."""
	inputs = inputs or {}
	views = inputs.get("booking_page_views")
	got = {r["platform"]: r for r in inputs.get("reviews") or []}
	return {
		"has_inputs": bool(inputs),
		"booking_page_views": views,
		"booking_reservations": booking_reservations,
		"booking_conversion_pct": _pct(booking_reservations, views) if views else None,
		"reviews": [{"platform": p, "scale": s,
		             "score": (got.get(p) or {}).get("score"),
		             "count": (got.get(p) or {}).get("count")} for p, s in PLATFORMS],
		"ads": [{"name": a["name"], "channel": a["channel"], "spend": _round(a["spend"]),
		         "sales": _round(a["sales"]),
		         "roas": _round(a["sales"] / a["spend"], 1) if a["spend"] else None}
		        for a in inputs.get("ads") or []],
		"payment_costs": [{"channel": p["channel"], "amount": _round(p["amount"])}
		                  for p in inputs.get("payment_costs") or []],
	}
