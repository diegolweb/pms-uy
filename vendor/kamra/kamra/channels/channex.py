"""Channex.io adapter - https://docs.channex.io

API-first channel-manager infrastructure: self-serve signup, sandbox at
staging.channex.io, per-property pricing, certified with Booking.com,
Agoda, Expedia, Airbnb and more. The hotel brings its own Channex
account; api_key is a Channex user API key, external_property_id the
Channex property id, and mappings carry Channex room_type/rate_plan ids.

Bookings come in as booking revisions. The webhook (event: booking, at
/api/method/kamra.channel_manager.webhook?connection=<name>, with the same
secret on both sides or a signing key for its HMAC) is only a signal; the
worker reads the unacked revisions from the feed, applies each one, commits
and only then acks it - Channex wipes the guest's contact data from an
acked revision, and keeps an unacked one in the feed.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from frappe import _

DEFAULT_ENDPOINT = "https://app.channex.io/api/v1"
FEED_PAGE_LIMIT = 100


class ChannexError(Exception):
	"""A non-2xx answer from Channex. detail is short (<= 300 characters) and
	never the whole body."""

	def __init__(self, status: int, detail: str):
		self.status = status
		self.detail = str(detail or "")[:300]
		super().__init__(f"Channex {status}: {self.detail}")


class ChannexRateLimited(ChannexError):
	"""429: wait retry_after seconds (Retry-After; 60 when missing)."""

	def __init__(self, status: int, detail: str, retry_after: int = 60):
		super().__init__(status, detail)
		self.retry_after = retry_after


def _retry_after(value) -> int:
	try:
		seconds = int(str(value).strip())
	except (TypeError, ValueError):
		return 60
	return seconds if seconds > 0 else 60


def _request(conn, method: str, path: str, *, params: dict | None = None,
             payload=None) -> dict:
	"""One call to the Channex API: the JSON of a 2xx, ChannexRateLimited on a
	429, ChannexError on anything else."""
	import requests

	key = conn.get_password("api_key", raise_exception=False)
	base = (conn.endpoint or DEFAULT_ENDPOINT).rstrip("/")
	res = requests.request(
		method, f"{base}/{path}", params=params, json=payload,
		headers={"user-api-key": key or ""}, timeout=20)
	if 200 <= res.status_code < 300:
		try:
			return res.json()
		except ValueError:
			return {}
	try:
		body = res.json()
		detail = str(body.get("errors") or body) if isinstance(body, dict) else str(body)
	except ValueError:
		detail = res.text or ""
	if res.status_code == 429:
		raise ChannexRateLimited(429, detail, _retry_after(res.headers.get("Retry-After")))
	raise ChannexError(res.status_code, detail)


def _attributes(item: dict) -> dict:
	return dict(item.get("attributes") or {}, id=item.get("id"))


def fetch_feed(conn, page: int = 1, limit: int = FEED_PAGE_LIMIT) -> list[dict]:
	"""The unacked revisions of the connection's property, oldest first."""
	body = _request(conn, "GET", "booking_revisions/feed", params={
		"filter[property_id]": conn.external_property_id,
		"order[inserted_at]": "asc",
		"pagination[page]": page,
		"pagination[limit]": limit,
	})
	return [_attributes(item) for item in body.get("data") or []]


def fetch_revision(conn, revision_id: str) -> dict:
	return _attributes(_request(conn, "GET", f"booking_revisions/{revision_id}").get("data") or {})


def ack_revision(conn, revision_id: str) -> None:
	_request(conn, "POST", f"booking_revisions/{revision_id}/ack")


def verify_signature(secret: str, timestamp: str, signatures: str, raw_body: bytes,
                     now: float | None = None, tolerance: int = 300) -> bool:
	"""Channex's webhook HMAC: hex(HMAC_SHA256(secret, f"{timestamp}.{raw_body}"))
	in x-hmac-signature-256 (two, comma-separated, while a key rotates), with
	x-hmac-timestamp no more than `tolerance` seconds away."""
	if not (secret and timestamp and signatures):
		return False
	try:
		sent_at = int(str(timestamp).strip())
	except ValueError:
		return False
	if abs((time.time() if now is None else now) - sent_at) > tolerance:
		return False
	expected = hmac.new(str(secret).encode(), f"{sent_at}.".encode() + (raw_body or b""),
	                    hashlib.sha256).hexdigest()
	return any(hmac.compare_digest(expected, s.strip().lower())
	           for s in str(signatures).split(","))


def _call(conn, method: str, path: str, payload=None):
	import requests

	key = conn.get_password("api_key", raise_exception=False)
	base = (conn.endpoint or DEFAULT_ENDPOINT).rstrip("/")
	res = requests.request(
		method, f"{base}/{path}",
		json=payload, headers={"user-api-key": key or ""}, timeout=20)
	ok = 200 <= res.status_code < 300
	try:
		body = res.json()
	except Exception:
		body = {"raw": res.text[:300]}
	return ok, body


def push_ari(conn, snapshot) -> tuple[bool, str]:
	"""Channex takes availability and rate values as bulk arrays."""
	availability, rates = [], []
	for rt in snapshot:
		for day in rt["days"]:
			availability.append({
				"property_id": conn.external_property_id,
				"room_type_id": rt["external_room_id"],
				"date": day["date"],
				"availability": day["available"],
			})
			if rt.get("external_rate_id"):
				rates.append({
					"property_id": conn.external_property_id,
					"rate_plan_id": rt["external_rate_id"],
					"date": day["date"],
					"rate": str(day["rate"]),
				})
	ok1, b1 = _call(conn, "POST", "availability",
	                {"values": availability})
	ok2, b2 = (True, {}) if not rates else _call(
		conn, "POST", "restrictions", {"values": rates})
	if ok1 and ok2:
		return True, f"{len(availability)} availability, {len(rates)} rate values"
	err = (b1 if not ok1 else b2)
	return False, str(err)[:280]


def _amount(value) -> float:
	return float(value or 0)


def _notes(rev: dict) -> str:
	"""What reception needs from the revision: the guest's notes, the arrival
	time and the extra services. Never the card guarantee."""
	lines = [rev.get("notes") or ""]
	if rev.get("arrival_hour"):
		lines.append(_("Arrival time: {0}").format(rev["arrival_hour"]))
	for s in rev.get("services") or []:
		lines.append(_("Service: {0} × {1}, {2} {3}").format(
			s.get("name") or s.get("type") or "", s.get("persons") or "",
			s.get("total_price") or "", rev.get("currency") or ""))
	return "\n".join(line for line in lines if line)


def revision_events(revision: dict) -> list[dict]:
	"""A Channex booking revision -> Kamra's normalized events.

	One event per room; ota_ref is the OTA's reservation code for a single
	room and "<code>-<n>" for each room of a multi-room booking (the AioSell
	convention), so each room is its own idempotent reservation and a cancel
	reaches them all through booking_id."""
	rev = revision or {}
	booking_id = str(rev.get("ota_reservation_code") or rev.get("unique_id")
	                 or rev.get("booking_id") or "")
	channel = rev.get("ota_name") or "OTA"
	revision_id = rev.get("id") or ""
	status = (rev.get("status") or "new").lower()
	if status == "cancelled":
		return [{"event": "cancel", "booking_id": booking_id, "ota_ref": booking_id,
		         "channel": channel, "revision_id": revision_id}]

	event = "modify" if status == "modified" else "book"
	customer = rev.get("customer") or {}
	guest_name = " ".join(p for p in (customer.get("name"), customer.get("surname")) if p).strip()
	rooms = rev.get("rooms") or []
	multi = len(rooms) > 1
	commission = _amount(rev.get("ota_commission"))
	rooms_total = sum(_amount(r.get("amount")) for r in rooms)
	notes = _notes(rev)

	out = []
	for i, room in enumerate(rooms):
		occ = room.get("occupancy") or {}
		days = room.get("days") or {}
		if multi:
			total = _amount(room.get("amount")) or sum(_amount(v) for v in days.values())
			share = round(commission * total / rooms_total, 2) if rooms_total else 0.0
		else:
			total = _amount(rev.get("amount")) or _amount(room.get("amount"))
			share = commission
		out.append({
			"event": event,
			"booking_id": booking_id,
			"ota_ref": f"{booking_id}-{i}" if multi else booking_id,
			"revision_id": revision_id,
			"channel": channel,
			"room_type_external_id": room.get("room_type_id") or "",
			"external_rate_id": room.get("rate_plan_id"),
			"check_in": room.get("checkin_date") or rev.get("arrival_date"),
			"check_out": room.get("checkout_date") or rev.get("departure_date"),
			"adults": int(occ.get("adults") or 2),
			"children": int(occ.get("children") or 0),
			"guest_name": guest_name or "OTA Guest",
			"phone": customer.get("phone") or "",
			"email": customer.get("mail") or "",
			"total": total,
			"nightly": [{"date": d, "amount": _amount(v)} for d, v in sorted(days.items())],
			"currency": rev.get("currency") or "",
			"commission": share,
			"notes": notes,
		})
	return out


def parse_webhook(conn, payload) -> list[dict]:
	"""Kept for callers of the provider seam. Channex's booking webhook is a
	signal ({event, payload: {booking_id, property_id, revision_id}}) and
	parses to nothing - the booking comes from the feed. A payload that does
	carry the revision (send_data) parses as that revision."""
	body = (payload or {}).get("payload") or {}
	if not body.get("rooms"):
		return []
	return revision_events(body)
