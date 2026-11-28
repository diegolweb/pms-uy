# Copyright (c) 2026, Hotel Roosevelt and contributors
# For license information, please see license.txt

"""Indicators (HR-379): the marketing plan's figures on one screen
(/indicators). Only the reads live here; every rule is in
kamra.indicator_math, which tests without a site."""

import json
from datetime import date, datetime, timedelta

import frappe
from frappe.utils import getdate

from kamra import indicator_math as im
from kamra.authz import require_perm


def _region(phone: str | None) -> str | None:
	"""ISO-2 region of an E.164 phone, or None."""
	if not phone:
		return None
	try:
		import phonenumbers

		code = phonenumbers.region_code_for_number(phonenumbers.parse(phone, None))
	except Exception:
		return None
	return code if code and code not in ("ZZ", "001") else None


def _bounds(period: str) -> tuple[date, date]:
	try:
		start = datetime.strptime(period + "-01", "%Y-%m-%d").date()
	except (TypeError, ValueError):
		frappe.throw("period must be YYYY-MM")
	nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
	return start, nxt


def _rows(property: str, start: date, end: date, nxt: date) -> list[im.Res]:
	"""The stays touching [start, end), the arrivals in it and the OTA
	bookings made in the calendar month."""
	data = frappe.db.sql(
		"""SELECT r.name, r.guest, r.status, r.creation, r.check_in_date, r.check_out_date,
		          r.amount_before_tax, r.amount_after_tax, r.tax_amount, r.commission_amount,
		          r.source, r.channel, r.travel_agent, r.cancellation_note, r.booked_by_phone,
		          g.phone AS guest_phone
		   FROM `tabReservation` r
		   LEFT JOIN `tabGuest` g ON g.name = r.guest
		   WHERE r.property = %(p)s
		     AND ((r.check_in_date < %(e)s AND r.check_out_date > %(s)s)
		          OR (r.check_in_date >= %(s)s AND r.check_in_date < %(e)s)
		          OR (r.source = 'OTA' AND r.creation >= %(s)s AND r.creation < %(n)s))""",
		{"p": property, "s": str(start), "e": str(end), "n": str(nxt)}, as_dict=True)
	out = []
	for x in data:
		ci = getdate(x.check_in_date)
		base = im.base_of(x.amount_before_tax, x.amount_after_tax, x.tax_amount)
		out.append(im.Res(
			name=x.name, guest=x.guest, status=x.status, creation=x.creation,
			check_in=ci, check_out=getdate(x.check_out_date) if x.check_out_date else ci,
			base=base, commission=float(x.commission_amount or 0), source=x.source,
			channel=x.channel, travel_agent=x.travel_agent,
			cancellation_note=x.cancellation_note,
			market=_region(x.guest_phone or x.booked_by_phone)))
	return out


def _history(property: str, guests: set[str]) -> list[tuple]:
	if not guests:
		return []
	return [tuple(r) for r in frappe.db.sql(
		"""SELECT name, guest, status, check_in_date FROM `tabReservation`
		   WHERE property = %(p)s AND guest IN %(g)s
		     AND status IN ('Checked In', 'Checked Out')""",
		{"p": property, "g": tuple(sorted(guests))})]


def _consent(property: str, start: date, nxt: date) -> tuple[int, int]:
	"""Guests of this hotel who said yes to marketing and can be reached, and
	how many said yes this month."""
	args = {"p": property, "s": str(start), "n": str(nxt)}
	total = frappe.db.sql(
		"""SELECT COUNT(DISTINCT g.name) FROM `tabGuest` g
		   WHERE g.marketing_consent = 1
		     AND (IFNULL(g.email, '') != '' OR IFNULL(g.phone, '') != '')
		     AND EXISTS (SELECT 1 FROM `tabReservation` r
		                 WHERE r.guest = g.name AND r.property = %(p)s)""", args)[0][0]
	new = frappe.db.sql(
		"""SELECT COUNT(DISTINCT g.name) FROM `tabGuest` g
		   WHERE g.marketing_consent = 1
		     AND (IFNULL(g.email, '') != '' OR IFNULL(g.phone, '') != '')
		     AND g.marketing_consent_date >= %(s)s AND g.marketing_consent_date < %(n)s
		     AND EXISTS (SELECT 1 FROM `tabReservation` r
		                 WHERE r.guest = g.name AND r.property = %(p)s)""", args)[0][0]
	return int(total or 0), int(new or 0)


def _inputs(property: str, period: str) -> dict | None:
	raw = frappe.db.get_value("Indicator Month", {"property": property, "period": period}, "inputs")
	if not raw:
		return None
	return json.loads(raw) if isinstance(raw, str) else raw


def _stats(s: dict) -> tuple:
	sold = s["rooms_sold"]
	return s["occupancy_pct"], (s["adr"] if sold else None)


@frappe.whitelist()
@require_perm("Property", "view_performance")
def indicators(property: str, period: str | None = None) -> dict:
	"""The marketing plan's indicators: the last 7 days and what is booked
	ahead, the month so far (period 'YYYY-MM', this month by default) by
	channel and market, and the figures entered from outside Kamra."""
	from kamra.channel_manager import channel_commission_pcts
	from kamra.dashboards import occupancy_forecast
	from kamra.no_show import property_now
	from kamra.reports import range_stats

	today = property_now(property).date()
	period = period or today.strftime("%Y-%m")
	start, nxt = _bounds(period)
	period = start.strftime("%Y-%m")
	end = min(nxt, today)
	rooms = frappe.db.count("Room", {"property": property}) or 0

	week = range_stats(property, today - timedelta(days=7), today - timedelta(days=1), rooms)
	w_occ, w_adr = _stats(week)
	out = {
		"today": str(today), "period": period,
		"window": {"from": str(start), "to": str(end - timedelta(days=1))} if end > start else None,
		"rooms": rooms,
		"week": {"from": str(today - timedelta(days=7)), "to": str(today - timedelta(days=1)),
		         "occupancy_pct": w_occ, "adr": w_adr},
		"on_books": im.on_books(occupancy_forecast(property, str(today), 90), today),
		"month": None, "channels": [], "totals": None, "missing_commission": [],
		"lead_time": [], "lead_time_total": None,
	}

	rows = _rows(property, start, max(end, start), nxt)
	inputs = _inputs(property, period)
	out["outside"] = im.outside(inputs, im.booking_reservations(rows, start, nxt))
	if end <= start:
		return out

	table = im.channel_table(rows, start, end, channel_commission_pcts(), inputs)
	month = range_stats(property, start, end - timedelta(days=1), rooms)
	m_occ, m_adr = _stats(month)
	arrived = {r.guest for r in rows if r.guest and start <= r.check_in < end}
	returning = im.returning_names(rows, start, end, _history(property, arrived))
	repeat = im.repeat_share(rows, start, end, returning)
	consent, consent_new = _consent(property, start, nxt)
	lead, lead_total = im.lead_times(rows, start, end)
	out.update({
		"month": {"occupancy_pct": m_occ, "rooms_sold": month["rooms_sold"],
		          "room_nights_available": rooms * (end - start).days,
		          "adr": m_adr, "revpar": month["revpar"],
		          "direct_nights_pct": table["direct_nights_pct"],
		          "direct_revenue_pct": table["direct_revenue_pct"],
		          **repeat, "consent_list": consent, "consent_new": consent_new},
		"channels": table["channels"], "totals": table["totals"],
		"missing_commission": table["missing_commission"],
		"lead_time": lead, "lead_time_total": lead_total,
	})
	return out


@frappe.whitelist(methods=["POST"])
@require_perm("Revenue Budget", "set_budget")
def save_month_inputs(property: str, period: str, inputs: str | dict) -> dict:
	"""Store the month's figures from outside Kamra (Booking page views,
	reviews, ads, payment costs), checked by indicator_math.clean_inputs.
	Whoever sets the budget targets enters them."""
	from kamra.channel_manager import channel_commission_pcts
	from kamra.no_show import property_now

	start, nxt = _bounds(period)
	period = start.strftime("%Y-%m")
	if isinstance(inputs, str):
		try:
			inputs = json.loads(inputs)
		except ValueError:
			frappe.throw("inputs must be JSON")
	end = max(min(nxt, property_now(property).date()), start)
	table = im.channel_table(_rows(property, start, end, nxt), start, end,
	                         channel_commission_pcts(), None)
	try:
		clean = im.clean_inputs(inputs, {c["key"] for c in table["channels"]})
	except ValueError as e:
		frappe.throw(str(e))
	name = frappe.db.get_value("Indicator Month", {"property": property, "period": period})
	doc = frappe.get_doc("Indicator Month", name) if name else frappe.new_doc("Indicator Month")
	doc.update({"property": property, "period": period, "inputs": json.dumps(clean)})
	doc.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persists the completed operation before returning to an external/public caller; reviewed as intentional
	return {"ok": True}
