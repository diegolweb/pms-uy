"""Booking pace (pickup) - pure rules, no Frappe.

Room nights on the books for the coming weeks or months, and how many came
in over the last 7 and 30 days. "Came in" is the net: new sales minus
cancellations, plus or minus stays whose dates moved. The state of a
reservation at an earlier moment is rebuilt from its change log (Version
rows): the current status and dates, with every later change undone.

The endpoint (`kamra.reports.pickup_pace`) only reads the rows; every rule
lives here so it can be tested without a site.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise

SOLD = frozenset({"Confirmed", "Checked In", "Checked Out", "No Show"})
HOLD_EXPIRY_NOTE = "Hold / payment window expired"  # = reservation_state.HOLD_EXPIRY_NOTE
DECLINED_PREFIX = "Request declined:"  # booking_requests.decline_request

WEEKS = 13
MONTHS = 6
_TRACKED = ("status", "check_in_date", "check_out_date")
_COUNTED = ("behind", "on_pace", "covered")


@dataclass(frozen=True)
class Stay:
	name: str
	status: str
	creation: datetime
	cancelled_on: datetime | None
	cancellation_note: str | None
	check_in: date
	check_out: date
	source: str | None
	channel: str | None


@dataclass(frozen=True)
class Change:
	"""One Version row of the reservation: when it was saved and its JSON."""

	at: datetime
	data: str


def _rows(change: Change) -> list:
	"""The `changed` rows of a Version ([field, old, new]); unreadable -> []."""
	try:
		rows = (json.loads(change.data or "{}") or {}).get("changed") or []
	except (TypeError, ValueError, AttributeError):
		return []
	return [r for r in rows if isinstance(r, (list, tuple)) and len(r) >= 3 and r[0] in _TRACKED]


def _day(value) -> date | None:
	try:
		return date.fromisoformat(str(value)[:10])
	except (TypeError, ValueError):
		return None


def _never_sold(stay: Stay) -> bool:
	note = (stay.cancellation_note or "").strip()
	return note == HOLD_EXPIRY_NOTE or note.startswith(DECLINED_PREFIX)


def state_at(stay: Stay, changes: list[Change], at: datetime) -> tuple[date, date] | None:
	"""(check-in, check-out) if the reservation was sold at `at`; None if not.

	Starts from the current status and dates and undoes, newest first, every
	change saved after `at`. A cancelled stay without any status change in its
	log (older data, or a path that wrote without saving) counts from its
	creation to `cancelled_on`, unless it was an expired hold or a declined
	request, which were never sold."""
	if stay.creation > at:
		return None
	status, check_in, check_out = stay.status, stay.check_in, stay.check_out
	parsed = [(c.at, _rows(c)) for c in changes]
	for when, rows in sorted(parsed, key=lambda p: p[0], reverse=True):
		if when <= at:
			continue
		for field, old, _new in (r[:3] for r in rows):
			if field == "status":
				status = old
			elif field == "check_in_date":
				check_in = _day(old) or check_in
			else:
				check_out = _day(old) or check_out
	if stay.status == "Cancelled" and not any(r[0] == "status" for _, rows in parsed for r in rows):
		if _never_sold(stay) or (stay.cancelled_on is not None and at >= stay.cancelled_on):
			return None
		return check_in, check_out
	return (check_in, check_out) if status in SOLD else None


def nights_in(check_in: date, check_out: date, start: date, end: date) -> int:
	"""Nights of a stay inside [start, end) - end exclusive; a day use is 0."""
	return max(0, (min(check_out, end) - max(check_in, start)).days)


def _next_month(d: date) -> date:
	return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def periods(start: date, grain: str) -> list[tuple[date, date]]:
	"""13 weeks (Monday to Monday) or 6 calendar months; the first one runs from
	`start` to the next Monday / the 1st of the next month. Ends are exclusive."""
	if grain == "week":
		first = start + timedelta(days=7 - start.weekday())
		bounds = [start] + [first + timedelta(weeks=i) for i in range(WEEKS)]
	elif grain == "month":
		bounds = [start]
		for _ in range(MONTHS):
			bounds.append(_next_month(bounds[-1]))
	else:
		raise ValueError(f"grain must be 'week' or 'month', not {grain!r}")
	return list(pairwise(bounds))


def channel_of(stay: Stay) -> tuple[str, str]:
	"""(label, kind): the OTA's channel name, else the source (which the
	frontend translates)."""
	if stay.source == "OTA":
		return (stay.channel or "OTA"), "channel"
	return (stay.source or "Manual"), "source"


def _target_nights(start: date, end: date, rooms: int, targets: dict[str, float]) -> float | None:
	"""Rooms x that month's occupancy target, night by night; None if any month
	of the period has no target."""
	total = 0.0
	d = start
	while d < end:
		pct = targets.get(d.strftime("%Y-%m")) or 0
		if pct <= 0:
			return None
		total += rooms * pct / 100
		d += timedelta(days=1)
	return total


def _pct(nights: float, capacity: int) -> float:
	return round(100 * nights / capacity, 1) if capacity else 0.0


def summary(rows: list[dict]) -> dict:
	"""The overview over the same rows the table shows: sums, and occupancy,
	pace and target weighted by nights (each over the rows' total capacity)."""
	capacity = sum(r["capacity"] for r in rows)
	on_books = sum(r["on_books"] for r in rows)
	# a past period keeps what it sold: no more pickup can land on it
	projected = round(sum(r["on_books"] if r["projected_nights"] is None else r["projected_nights"]
	                      for r in rows), 1)
	targets = [r["target_nights"] for r in rows]
	target = None if (not rows or any(t is None for t in targets)) else round(sum(targets), 1)
	return {
		"capacity": capacity,
		"on_books": on_books,
		"occupancy_pct": _pct(on_books, capacity),
		"pickup_7": sum(r["pickup_7"] for r in rows),
		"pickup_30": sum(r["pickup_30"] for r in rows),
		"projected_nights": projected,
		"projected_pct": _pct(projected, capacity),
		"target_nights": target,
		"target_pct": None if target is None else _pct(target, capacity),
		"status": {s: sum(1 for r in rows if r["status"] == s) for s in _COUNTED},
	}


def pace(stays: list[Stay], changes: dict[str, list[Change]], *, now: datetime, today: date, start: date,
         grain: str, rooms: int, targets: dict[str, float]) -> dict:
	"""The booking-pace report (without `as_of`, which the endpoint adds).

	`targets` is {"YYYY-MM": occupancy_target > 0}. `now` is the site clock
	(the one `creation` is written in); `today` is the property's date."""
	spans = periods(start, grain)
	moments = (now, now - timedelta(days=7), now - timedelta(days=30))
	books = [[0, 0, 0] for _ in spans]
	by_channel: dict[str, dict] = {}
	for stay in stays:
		log = changes.get(stay.name) or []
		states = [state_at(stay, log, m) for m in moments]
		label, kind = channel_of(stay)
		row = by_channel.setdefault(label, {"label": label, "kind": kind, "on_books": 0, "pickup_7": 0,
		                                    "pickup_30": 0})
		for i, (s, e) in enumerate(spans):
			n = [nights_in(st[0], st[1], s, e) if st else 0 for st in states]
			for k in range(3):
				books[i][k] += n[k]
			row["on_books"] += n[0]
			row["pickup_7"] += n[0] - n[1]
			row["pickup_30"] += n[0] - n[2]

	periods_out = []
	for (s, e), (now_n, n7, n30) in zip(spans, books, strict=True):
		capacity = rooms * (e - s).days
		pickup_7 = now_n - n7
		target = _target_nights(s, e, rooms, targets)
		if e <= today:
			projected, status = None, "past"
		else:
			weeks = max(0, (s - today).days) / 7
			projected = min(capacity, now_n + max(0, pickup_7) * weeks)
			if target is None:
				status = "no_target"
			elif now_n >= target:
				status = "covered"
			elif projected >= target:
				status = "on_pace"
			else:
				status = "behind"
		periods_out.append({
			"start": s.isoformat(),
			"end": e.isoformat(),
			"capacity": capacity,
			"on_books": now_n,
			"occupancy_pct": _pct(now_n, capacity),
			"pickup_7": pickup_7,
			"pickup_30": now_n - n30,
			"projected_nights": None if projected is None else round(projected, 1),
			"projected_pct": None if projected is None else _pct(projected, capacity),
			"target_nights": None if target is None else round(target, 1),
			"target_pct": None if target is None else _pct(target, capacity),
			"status": status,
		})

	channels = sorted((c for c in by_channel.values() if c["on_books"] or c["pickup_7"] or c["pickup_30"]),
	                  key=lambda c: (-c["on_books"], c["label"]))
	return {
		"today": today.isoformat(),
		"start": start.isoformat(),
		"grain": grain,
		"rooms": rooms,
		"has_targets": all(p["target_nights"] is not None for p in periods_out),
		"periods": periods_out,
		"summary": summary(periods_out),
		"channels": channels,
		"totals": {k: sum(c[k] for c in channels) for k in ("on_books", "pickup_7", "pickup_30")},
	}
