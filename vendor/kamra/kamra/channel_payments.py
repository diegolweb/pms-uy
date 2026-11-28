# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

"""Who collects a channel booking's room money, and from when (HR-380).

A Channex revision says who takes the guest's money: the hotel
(`payment_collect` property, or none), the platform through a virtual card
it hands the hotel (`ota` + a card), or the platform itself, paying the
hotel by transfer (`ota` + `bank_transfer`). Kamra keeps that on the
Reservation (`ota_collect`), with the first day the hotel can charge it
(`ota_charge_from`) and the channel's last free-cancellation day
(`ota_free_cancel_until`), and reads it in the stay's balance, the deposit,
the check-in, the no-show fee and a channel cancel.

The hotel collects as soon as it can: a booking it collects is due the day
it comes in, not when the free cancellation ends. If the guest then cancels
in time, what was taken is given back from the folio (refund_folio_payment).
Kamra never stores or asks for card details.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import frappe
from frappe.utils import flt, getdate

from kamra.authz import assert_property_access, require_perm

COLLECTORS = ("Hotel", "Virtual card", "OTA")
HOTEL, VIRTUAL_CARD, PLATFORM = COLLECTORS
# the ones the hotel charges (the guest at the desk, or the platform's card)
HOTEL_COLLECTS = (HOTEL, VIRTUAL_CARD)
ENDED = ("Cancelled", "No Show")
OPEN = ("Confirmed", "Requested")
LIST_CAP = 200
_CENT = 0.005


# --- the rules (pure) ------------------------------------------------------------

# channels that always collect the guest's money themselves, whatever the
# revision says (D-S4-4: Airbnb pays the hotel out; Channex sends "AirBNB")
PLATFORM_ALWAYS = ("Airbnb",)


def collector_of(payment_collect: str | None, payment_type: str | None,
                 channel: str | None = None) -> str:
	"""R1: `ota` + `bank_transfer` -> the platform collects and pays the hotel;
	`ota` with a card (or none) -> the hotel charges the platform's virtual
	card; `property` or nothing -> the hotel collects from the guest. Airbnb
	always collects itself (D-S4-4)."""
	if channel:
		from kamra.channel_manager import canonical_channel
		if canonical_channel(channel) in PLATFORM_ALWAYS:
			return PLATFORM
	if (payment_collect or "").strip().lower() == "ota":
		if (payment_type or "").strip().lower() == "bank_transfer":
			return PLATFORM
		return VIRTUAL_CARD
	return HOTEL


def charge_from_of(collector: str, check_in, booked_on) -> date | None:
	"""R2: the first day the hotel can charge. Its own guest, the day the
	booking came in (D-HR-95: as soon as possible); the virtual card, the
	arrival (the card's real date only reaches PCI partners); the platform,
	never."""
	if collector == HOTEL:
		return getdate(booked_on) if booked_on else None
	if collector == VIRTUAL_CARD:
		return getdate(check_in) if check_in else None
	return None


def _utc_instant(value: str) -> datetime | None:
	"""An ISO instant from Channex (UTC, with or without the offset)."""
	from zoneinfo import ZoneInfo
	text = str(value).strip().replace("Z", "+00:00")
	try:
		at = datetime.fromisoformat(text)
	except ValueError:
		return None
	if at.tzinfo is None:
		at = at.replace(tzinfo=ZoneInfo("UTC"))
	return at


def free_cancel_until_of(penalty_from: str | None, check_in, free_cancel_days: int | None,
                         booked_on, tz: str | None) -> date | None:
	"""R2b: the last day the guest cancels free. From the channel's first
	charged cancel penalty (the local day, in the property's zone, of the
	second before it starts); without one, Kamra's own rule (free while
	there are at least free_cancel_days to the arrival). None when the
	booking was never free to cancel (non-refundable) or a fact is missing."""
	booked = getdate(booked_on) if booked_on else None
	if penalty_from:
		start = _utc_instant(penalty_from)
		if start is None:
			return None
		from zoneinfo import ZoneInfo
		try:
			zone = ZoneInfo((tz or "").strip() or "UTC")
		except Exception:
			zone = ZoneInfo("UTC")
		until = (start - timedelta(seconds=1)).astimezone(zone).date()
	else:
		if not check_in or free_cancel_days is None:
			return None
		until = getdate(check_in) - timedelta(days=int(free_cancel_days))
	if booked and until < booked:
		return None
	return until


# --- on the Reservation ----------------------------------------------------------

def _property_tz(property: str) -> str:
	from frappe.utils import get_system_timezone
	return (frappe.db.get_value("Property", property, "timezone") or "").strip() \
		or get_system_timezone()


def _local_today(property: str) -> date:
	from kamra.no_show import property_now
	return property_now(property).date()


def _booked_on(doc) -> date:
	"""The local day the booking came in: today for a new one, the day of
	its creation (site clock -> the property's zone) for a modify, so a
	modify never moves a Hotel booking's charge date later."""
	if doc.is_new() or not doc.get("creation"):
		return _local_today(doc.property)
	from zoneinfo import ZoneInfo

	from frappe.utils import get_datetime, get_system_timezone
	created = get_datetime(doc.creation)
	try:
		created = created.replace(tzinfo=ZoneInfo(get_system_timezone())) \
			.astimezone(ZoneInfo(_property_tz(doc.property)))
	except Exception:
		pass
	return created.date()


def apply_to(doc, e: dict) -> None:
	"""R8: who collects, from when and the free-cancellation day, on a
	Reservation an alta or a modify from Channex is about to save (the
	caller saves). is_pay_at_hotel follows: 1 only when the hotel collects
	from the guest. Money already taken is not touched."""
	collector = collector_of(e.get("payment_collect"), e.get("payment_type"),
	                         e.get("channel") or doc.get("channel"))
	booked_on = _booked_on(doc)
	free_days = frappe.db.get_value("Property", doc.property, "free_cancel_days")
	doc.ota_collect = collector
	doc.ota_charge_from = charge_from_of(collector, doc.check_in_date, booked_on)
	doc.ota_free_cancel_until = free_cancel_until_of(
		e.get("penalty_from"), doc.check_in_date,
		None if free_days is None else int(free_days), booked_on,
		_property_tz(doc.property))
	doc.is_pay_at_hotel = 1 if collector == HOTEL else 0


def _iso(value) -> str | None:
	return str(getdate(value)) if value else None


def collection(res, money: dict) -> dict | None:
	"""What the drawer says about a channel booking's money: None for a
	booking with no ota_collect; else who collects and the state -
	ota_paid (the platform collected), to_collect (something is owed: before
	arrival, or a no-show / late-cancel fee), collected, refund_due (R10: a
	cancelled stay in credit) or settled (ended, nothing to take or give)."""
	collector = res.get("ota_collect")
	if not collector:
		return None
	money = money or {}
	due = round(flt(money.get("due")), 2)
	status = res.get("status")
	ended = status in ENDED
	refund = 0.0
	if collector == PLATFORM:
		state = "settled" if ended else "ota_paid"
	elif status == "Cancelled" and due < -_CENT:
		state, refund = "refund_due", round(-due, 2)
	elif due > _CENT:
		state = "to_collect"
	elif ended:
		state = "settled"
	else:
		state = "collected"
	return {
		"collector": collector,
		"channel": res.get("channel"),
		"charge_from": _iso(res.get("ota_charge_from")),
		"free_cancel_until": _iso(res.get("ota_free_cancel_until")),
		"state": state,
		"due": due,
		"refund": refund,
	}


def platform_payment_key(reservation: str) -> str:
	"""The idempotency key of the OTA Prepaid line check-in puts on a
	platform-collected booking's folio (post_platform_payment)."""
	return f"ota-collect:{reservation}"


def post_platform_payment(res) -> str | None:
	"""R5: at check-in of a booking the platform collected, the room money
	it took goes on the guest folio as an OTA Prepaid payment (booking total
	less any advance), once - a repeated or redone check-in finds it by its
	idempotency key. Returns the payment line, or None when there is
	nothing to post.

	Written the way a payment link settles (payments.settle_payment_link),
	not through add_folio_payment: no money comes into anyone's till, so
	there is no till entry, no open till to ask for and no cashier PIN,
	and the permission is the check-in's (Reservation check_in_out, which
	the Night Auditor has and add_folio_payment's Folio bill is not). The
	Guest ledger still gets its credit, so the folio and its ledger agree.
	Not whitelisted: check_in and the kamra_uy patch call it."""
	if res.get("ota_collect") != PLATFORM:
		return None
	amount = round(flt(res.amount_after_tax) - flt(res.advance_paid), 2)
	if amount <= _CENT:
		return None
	from kamra.folio import _recalculate, open_folio
	folio = open_folio(res)
	# serialize posts on this folio so a second check-in sees the first
	frappe.db.get_value("Folio", folio, "name", for_update=True)
	from kamra.api import _paid_with_key
	key = platform_payment_key(res.name)
	landed = _paid_with_key(key, reservation=res.name)
	if landed:
		return frappe.db.get_value(
			"Folio Payment", {"parent": landed, "idempotency_key": key}, "name")
	doc = frappe.get_doc("Folio", folio)
	from kamra.localization import assert_payment_mode, default_currency, pack_for
	assert_payment_mode(doc.property, "OTA Prepaid")
	from kamra.business_date import get_business_date
	channel = res.get("channel") or "OTA"
	reference = " ".join(s for s in (res.get("channel"), res.get("ota_ref")) if s)
	doc.append("payments", {
		"posting_date": get_business_date(doc.property),
		"payment_kind": "Payment",
		"mode": "OTA Prepaid",
		"amount": amount,
		"reference": reference[:140] or None,
		# the Property's currency, never the INR schema default (#341)
		"currency": (frappe.get_cached_value("Property", doc.property, "currency")
		             or default_currency(pack_for(doc.property))),
		"idempotency_key": key,
	})
	_recalculate(doc)
	doc.save(ignore_permissions=True)
	row = doc.payments[-1]
	try:
		from kamra.ledger import record_payment_ledger
		record_payment_ledger(doc, row.as_dict())
	except frappe.QueryDeadlockError:
		raise  # the transaction is gone, the payment row with it
	except Exception:
		from kamra.privacy import guest_traceback
		frappe.log_error(title="ledger platform payment write failed",
		                 message=guest_traceback(doc))
	from kamra.localization import money_text
	from kamra.savings import log_action
	log_action("payment_received", "Folio", doc.name, doc.property,
	           rationale=f"{money_text(doc.property, amount)} paid by {channel} (OTA)",
	           agent_name="Channel", channel="API")
	return row.name



def _is_platform_key(key: str | None, reservation: str) -> bool:
	"""One of this booking's platform lines: the check-in's key, or a
	modification's (key + ":" + revision). Never a bare startswith: the key
	of RES-2026-00004 is a prefix of RES-2026-000041's."""
	base = platform_payment_key(reservation)
	return bool(key) and (key == base or key.startswith(base + ":"))


def platform_payment_lines(reservation: str) -> list[dict]:
	"""The OTA Prepaid lines the platform's money put on this booking's
	folios (check-in and modifications), oldest first: name, parent,
	amount, idempotency_key, folio_status."""
	rows = frappe.db.sql(
		"""SELECT fp.name, fp.parent, fp.amount, fp.idempotency_key,
		          f.status AS folio_status
		   FROM `tabFolio Payment` fp
		   JOIN `tabFolio` f ON f.name = fp.parent
		   WHERE fp.parenttype = 'Folio' AND f.reservation = %(res)s
		     AND fp.idempotency_key LIKE %(like)s
		   ORDER BY f.creation, fp.idx""",
		{"res": reservation, "like": platform_payment_key(reservation) + "%"}, as_dict=True)
	return [r for r in rows if _is_platform_key(r.idempotency_key, reservation)]


def _lock_folios(folios) -> None:
	"""Serialize with check-in, a modification and the undo on these folios."""
	for name in sorted(set(folios)):
		frappe.db.get_value("Folio", name, "name", for_update=True)


def _offset_guest_credit(doc, row_name: str, amount: float, description: str) -> None:
	"""Offset (part of) a platform line's credit on the Guest ledger with a
	debit that reverses it - the pattern of ledger.reverse_ledger_for_charge.
	Never more than is still credited for the row: a credit that never
	reached the ledger leaves nothing to offset. No try/except: if the
	ledger fails, the caller fails with it (money - better not to change
	the bill than to leave it apart from its ledger)."""
	from kamra.ledger import write_ledger_entry
	entries = frappe.get_all(
		"Folio Ledger Entry",
		filters={"folio": doc.name, "ledger": "Guest", "source_doctype": "Folio Payment",
		         "source_name": row_name},
		fields=["name", "credit", "debit", "transaction_code", "reversal_of"],
		order_by="creation asc")
	net = round(sum(flt(e.credit) - flt(e.debit) for e in entries), 2)
	debit = round(min(flt(amount), net), 2)
	if debit <= _CENT:
		return
	orig = next((e for e in entries if not e.reversal_of), None)
	write_ledger_entry(
		property=doc.property, folio=doc.name, ledger="Guest", debit=debit,
		description=description[:140],
		transaction_code=orig.transaction_code if orig else None,
		reversal_of=orig.name if orig else None,
		source_doctype="Folio Payment", source_name=row_name)


def reverse_platform_payment(res, reason: str) -> float:
	"""Undoing the check-in takes off every OTA Prepaid line the platform's
	money put on the booking's open folios (the check-in's and any
	modification's) and offsets what is still credited for each on the
	Guest ledger; the next check-in posts the line again by its key, so the
	bill keeps one line. Returns the amount taken off (0.0 when none).

	Not whitelisted: only reverse_check_in calls it, after its blocker
	(which lets these lines through only on an Open folio). No try/except
	around the ledger: if it fails the whole reversal fails."""
	_lock_folios(r.parent for r in platform_payment_lines(res.name))
	# read under the lock: a modification may have changed them meanwhile
	lines = [r for r in platform_payment_lines(res.name) if r.folio_status == "Open"]
	if not lines:
		return 0.0
	from kamra.folio import _recalculate
	total = 0.0
	removed: list[tuple] = []
	for folio in dict.fromkeys(r.parent for r in lines):
		doc = frappe.get_doc("Folio", folio)
		if doc.status != "Open":
			continue
		keep = {r.name for r in lines if r.parent == folio}
		rows = [p for p in doc.payments if p.name in keep]
		for p in rows:
			doc.remove(p)
		_recalculate(doc)
		doc.save(ignore_permissions=True)
		for p in rows:
			removed.append((doc, p))
			total += flt(p.amount)
	for doc, p in removed:
		_offset_guest_credit(doc, p.name, flt(p.amount), f"VOID: {p.mode} - check-in reversed")
	total = round(total, 2)
	if removed:
		doc = removed[0][0]
		channel = res.get("channel") or "OTA"
		from kamra.localization import money_text
		from kamra.savings import log_action
		log_action("payment_reversed", "Folio", doc.name, doc.property,
		           rationale=f"{money_text(doc.property, total)} paid by {channel} (OTA) "
		                     "taken off: check-in reversed",
		           agent_name="Channel", channel="Desk")
	return total


def adjust_platform_payment(res, revision: str | None) -> float:
	"""A channel modification with the guest in house brings the platform's
	payment on the bill up to date: the booking total less any advance (the
	check-in's sum, post_platform_payment) against the OTA Prepaid lines
	already there. A longer stay gets a new line keyed by the revision; a
	shorter one takes the difference off the newest lines (a row removed or
	lowered, its Guest credit offset) - never a Refund row, which would show
	money the hotel never paid back. Returns the difference posted (+/-),
	0.0 when there is none.

	A stay with no platform line on its folios and payments beyond its
	advance is left alone (logged): the desk took that money by hand, and
	the target would pay it twice. An advance alone (one that covered the
	whole stay at check-in) is already out of the target: it adjusts.

	Not whitelisted: only the channel's modify calls it. No try/except
	around the ledger: if it fails the revision fails, and the channel
	retries it (it is not acked)."""
	if res.get("ota_collect") != PLATFORM or res.status != "Checked In":
		return 0.0
	target = round(flt(res.amount_after_tax) - flt(res.advance_paid), 2)
	from kamra.folio import _recalculate, open_folio
	folio = open_folio(res)
	_lock_folios([folio, *(r.parent for r in platform_payment_lines(res.name))])
	lines = platform_payment_lines(res.name)  # read again under the lock
	if not lines and _hand_payments(res) > _CENT:
		# no platform line and money on the bill beyond the advance (the kamra_uy
		# patch skips a stay whose folio had a payment the desk took by hand):
		# posting the target would pay it twice, so nothing is adjusted - the
		# desk reviews it. An advance alone is already out of the target.
		frappe.log_error(title="platform payment not adjusted",
		                 message=f"{res.name}: no platform line on its folios")
		return 0.0
	diff = round(target - sum(flt(r.amount) for r in lines), 2)
	if abs(diff) <= _CENT:
		return 0.0
	channel = res.get("channel") or "OTA"
	if diff > 0:
		key = f"{platform_payment_key(res.name)}:{revision or frappe.generate_hash(length=10)}"
		from kamra.api import _paid_with_key
		if _paid_with_key(key, reservation=res.name):
			return 0.0  # the same revision again
		doc = frappe.get_doc("Folio", folio)
		_append_platform_line(doc, res, diff, key)
		_recalculate(doc)
		doc.save(ignore_permissions=True)
		from kamra.ledger import record_payment_ledger
		record_payment_ledger(doc, doc.payments[-1].as_dict())
	else:
		left = -diff
		docs: dict = {}
		cuts: list[tuple] = []
		for line in reversed(lines):
			if left <= _CENT:
				break
			if line.folio_status != "Open":
				continue
			if line.parent not in docs:
				docs[line.parent] = frappe.get_doc("Folio", line.parent)
			doc = docs[line.parent]
			row = next((p for p in doc.payments if p.name == line.name), None)
			if not row:
				continue
			amount = round(flt(row.amount), 2)
			cut = round(min(amount, left), 2)
			if amount - cut <= _CENT:
				doc.remove(row)
			else:
				row.amount = round(amount - cut, 2)
			cuts.append((doc, line.name, cut))
			left = round(left - cut, 2)
		for doc in docs.values():
			_recalculate(doc)
			doc.save(ignore_permissions=True)
		for doc, row_name, cut in cuts:
			_offset_guest_credit(doc, row_name, cut, f"VOID: OTA Prepaid - stay changed by {channel}")
		if left > _CENT:
			# what is left sits on a closed folio: an issued bill is not touched
			from kamra.privacy import guest_traceback
			frappe.log_error(title="platform payment adjustment left over",
			                 message=guest_traceback(res))
	from kamra.localization import money_text
	from kamra.savings import log_action
	log_action("payment_received" if diff > 0 else "payment_reversed", "Folio", folio,
	           res.property,
	           rationale=f"{money_text(res.property, abs(diff))} "
	                     f"{'more' if diff > 0 else 'less'} paid by {channel} (OTA): stay changed",
	           agent_name="Channel", channel="API")
	return diff


def _hand_payments(res) -> float:
	"""What the booking's folios hold in payments that are neither the
	platform's lines nor its advance: their sum less advance_paid (an
	advance stays on the folio as its Advance row)."""
	folios = frappe.get_all("Folio", filters={"reservation": res.name}, pluck="name")
	if not folios:
		return 0.0
	pays = frappe.get_all("Folio Payment", filters={"parent": ("in", folios), "parenttype": "Folio"},
	                      fields=["amount", "idempotency_key"])
	other = sum(flt(p.amount) for p in pays if not _is_platform_key(p.idempotency_key, res.name))
	return round(other - flt(res.advance_paid), 2)


def _append_platform_line(doc, res, amount: float, key: str) -> None:
	"""An OTA Prepaid line on the folio, written as post_platform_payment
	writes the check-in's."""
	from kamra.business_date import get_business_date
	from kamra.localization import assert_payment_mode, default_currency, pack_for
	assert_payment_mode(doc.property, "OTA Prepaid")
	reference = " ".join(s for s in (res.get("channel"), res.get("ota_ref")) if s)
	doc.append("payments", {
		"posting_date": get_business_date(doc.property),
		"payment_kind": "Payment",
		"mode": "OTA Prepaid",
		"amount": amount,
		"reference": reference[:140] or None,
		"currency": (frappe.get_cached_value("Property", doc.property, "currency")
		             or default_currency(pack_for(doc.property))),
		"idempotency_key": key,
	})


# --- the manager's list on Today -------------------------------------------------

def _row_state(r, money: dict, today: date) -> tuple[str, float] | None:
	"""R9 / R10: the list state of one candidate and its amount, or None."""
	due = round(flt(money.get("due")), 2)
	if r.status == "Cancelled" and due < -_CENT:
		return "refund", round(-due, 2)
	if due <= _CENT:
		return None
	if r.status == "No Show":
		return "no_show", due
	if r.status == "Cancelled":
		return "cancelled", due
	check_in = getdate(r.check_in_date)
	if check_in <= today:
		return "arrival", due
	charge_from = getdate(r.ota_charge_from) if r.ota_charge_from else None
	if charge_from and charge_from > today:
		return "upcoming", due
	return "now", due


@frappe.whitelist()
@require_perm("Reservation", "handle_stays")
def to_collect(property: str) -> dict:
	"""R9: the channel bookings the hotel collects that still owe money or
	are owed a refund - urgent first (refunds, no-shows, late cancels,
	arrivals not paid), then by arrival. Every one (up to 200); the board
	shows the first few."""
	assert_property_access(property)
	from kamra.api import _stay_balances
	today = _local_today(property)
	# The candidates, filtered and ordered in SQL so the cap never drops an
	# owed one behind settled history: a stay before arrival owing on the
	# booking total (what _stay_balances counts against it), or an ended one
	# whose guest folio is not at zero. _stay_balances then decides.
	rows = frappe.db.sql(
		"""SELECT r.name, r.guest_name, r.channel, r.ota_ref, r.status, r.ota_collect,
		          r.check_in_date, r.nights, r.ota_charge_from, r.ota_free_cancel_until
		   FROM `tabReservation` r
		   LEFT JOIN (SELECT reservation, SUM(payments_total) AS paid, SUM(balance) AS due
		              FROM `tabFolio` WHERE folio_type = 'Guest' GROUP BY reservation) f
		     ON f.reservation = r.name
		   WHERE r.property = %(p)s AND r.ota_collect IN %(who)s
		     AND ((r.status IN %(open)s
		           AND COALESCE(r.amount_after_tax, 0)
		               - COALESCE(f.paid, r.advance_paid, 0) > %(cent)s)
		       OR (r.status IN %(ended)s AND f.due IS NOT NULL AND ABS(f.due) > %(cent)s))
		   ORDER BY CASE WHEN r.status IN %(ended)s OR r.check_in_date <= %(today)s
		                 THEN 0 ELSE 1 END,
		            r.check_in_date, r.name
		   LIMIT %(cap)s""",
		{"p": property, "who": HOTEL_COLLECTS, "open": OPEN, "ended": ENDED,
		 "cent": _CENT, "today": today, "cap": LIST_CAP}, as_dict=True)
	money = _stay_balances([r.name for r in rows])
	folios = {}
	if rows:
		for f in frappe.get_all(
				"Folio", filters={"reservation": ("in", [r.name for r in rows]),
				                  "folio_type": "Guest"},
				fields=["name", "reservation"], order_by="creation asc"):
			folios[f.reservation] = f.name  # the newest guest folio wins
	out, owed, refunds = [], [0, 0.0], [0, 0.0]
	for r in rows:
		got = _row_state(r, money.get(r.name) or {}, today)
		if not got:
			continue
		state, amount = got
		tally = refunds if state == "refund" else owed
		tally[0] += 1
		tally[1] = round(tally[1] + amount, 2)
		ref = (r.ota_ref or "").strip()
		out.append({
			"name": r.name, "guest_name": r.guest_name, "channel": r.channel,
			"ota_ref_tail": f"…{ref[-3:]}" if ref else None,
			"status": r.status, "collector": r.ota_collect,
			"check_in_date": _iso(r.check_in_date), "nights": int(r.nights or 0),
			"charge_from": _iso(r.ota_charge_from),
			"free_cancel_until": _iso(r.ota_free_cancel_until),
			"amount": amount, "folio": folios.get(r.name), "state": state,
		})
	return {"count": owed[0], "total": owed[1],
	        "refunds": {"count": refunds[0], "total": refunds[1]},
	        "today": str(today), "rows": out}
