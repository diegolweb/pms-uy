"""Guest messages from the OTAs, read and answered from the desk.

Channex brings the guest conversations of Booking.com, Expedia and Airbnb
through one messages API (it needs Channex's messages application on the
property; without it every call answers 403). Kamra keeps an index only -
Channel Message Thread: ids, counts, who spoke last, the reservation - and
reads the messages themselves live from Channex when the desk opens a
conversation, so there is no second copy of what guests write.

The index is refreshed by Channex's `message` webhook (a signal, queued like
the booking feed) and by a pull every minute. A new guest message rings the
front-desk bell once; the first pass of a connection fills the index quietly.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from kamra.authz import require_perm

DT = "Channel Message Thread"
JOB = "ota-msgs::{connection}"
MAX_PAGES = 5
PREVIEW_CHARS = 140
SENDERS = ("guest", "property", "system")
_OFF = "kamra:ota_msgs_off:{connection}"
_LIVE = "kamra:ota_msgs_live:{connection}"
_ERROR = "kamra:ota_msgs_error:{connection}"
_SCOPE = {"thread": DT}
# what a browser may show from Kamra's own origin: raster images; anything
# else a guest sends is handed over as a download
_INLINE = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
_EXTENSIONS = dict(_INLINE, **{"application/pdf": ".pdf"})
_FIELDS = ["name", "provider", "reservation", "channex_booking_id", "message_count",
           "last_message_at", "last_sender", "is_closed", "no_reply_needed"]


def platform(provider: str | None) -> str:
	"""Booking.com, Expedia or Airbnb: the name staff know."""
	from kamra.channels.channex import MESSAGE_PROVIDERS
	return MESSAGE_PROVIDERS.get(provider or "") or provider or "OTA"


def _local(value) -> datetime | None:
	"""A Channex timestamp (UTC, ISO 8601) in the hotel's time, naive."""
	from frappe.utils.data import convert_utc_to_system_timezone
	if not value:
		return None
	try:
		stamp = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
	except ValueError:
		return None
	return convert_utc_to_system_timezone(stamp).replace(tzinfo=None)


def _latest(*values) -> datetime | None:
	stamps = [s for s in (_local(v) for v in values) if s]
	return max(stamps) if stamps else None


# ---------------------------------------------------------------------------
# Reservation link: Channex's booking UUID -> the revision the feed applied
# ---------------------------------------------------------------------------


def _reservations_in(results: str | None) -> list[str]:
	"""The reservations a Channel Booking Revision's results name, in order."""
	text = results or ""
	try:
		data = json.loads(text or "[]")
	except ValueError:
		# results is cut at 2000 characters: read the names that survived
		return [s for s in re.findall(r'"([^"\\]+)"', text)
		        if s not in ("result", "reservation", "reservations")]
	out = []
	for row in data if isinstance(data, list) else []:
		if not isinstance(row, dict):
			continue
		for key in ("reservation", "reservations"):
			value = row.get(key)
			out += [str(v) for v in (value if isinstance(value, list) else [value]) if v]
	return out


def _reservation_for(connection: str, booking_id: str) -> str | None:
	"""The reservation the feed made for this Channex booking, or None while
	its revision is not applied yet (the next pass tries again)."""
	if not booking_id:
		return None
	for results in frappe.get_all("Channel Booking Revision", filters={
			"connection": connection, "booking_id": booking_id,
			"status": ("in", ("Applied", "Acked"))},
			pluck="results", order_by="creation desc"):
		for name in _reservations_in(results):
			if frappe.db.exists("Reservation", name):
				return name
	return None


# ---------------------------------------------------------------------------
# The index: webhook + cron -> pull_messages
# ---------------------------------------------------------------------------


def enqueue_messages_pull(connection: str) -> None:
	"""One pull per connection at a time: the webhook and the cron share it."""
	frappe.enqueue("kamra.channel_messages.pull_messages", queue="short",
	               connection=connection, job_id=JOB.format(connection=connection),
	               deduplicate=True)


def enqueue_message_pulls() -> None:
	"""Every minute: a pull for each active Channex connection (catches a
	webhook that never came)."""
	for name in frappe.get_all("Channel Manager Connection",
	                           filters={"provider": "Channex", "active": 1}, pluck="name"):
		enqueue_messages_pull(name)


def _pause(connection: str, reason: str, seconds: int) -> None:
	frappe.cache.set_value(_OFF.format(connection=connection), reason,
	                       expires_in_sec=max(1, int(seconds)))


def _log_error(conn, exc: Exception, thread: str | None = None) -> None:
	"""One Error Log an hour per connection, with ids only: never the
	thread's title, a message or its attachments."""
	key = _ERROR.format(connection=conn.name)
	if frappe.cache.get_value(key):
		return
	frappe.cache.set_value(key, 1, expires_in_sec=3600)
	status = getattr(exc, "status", "")
	frappe.log_error(title=f"Channex messages {conn.name}",
	                 message=f"{type(exc).__name__} status={status} thread={thread or ''} "
	                         f"property={conn.external_property_id}")


def _guest_name(reservation: str | None, provider: str | None) -> str:
	name = frappe.db.get_value("Reservation", reservation, "guest_name") if reservation else None
	return name or _("Guest from {0}").format(platform(provider))


def _sync_thread(conn, item: dict, quiet: bool) -> bool:
	"""Upsert one thread of Channex's list into the index; True when it rang
	the bell (a new guest message, outside the quiet first pass)."""
	from kamra.channels import channex
	tid = str(item.get("id") or "")
	try:
		channex._thread_path(tid)
	except ValueError:
		return False
	attrs = item.get("attributes") or {}
	last = attrs.get("last_message") if isinstance(attrs.get("last_message"), dict) else {}
	sender = last.get("sender") if last.get("sender") in SENDERS else ""
	count = cint(attrs.get("message_count"))
	booking_id = channex._related_id(item, "booking")
	existing = frappe.db.get_value(DT, tid, _FIELDS + ["property", "notified_count"], as_dict=True)
	if existing and existing.property != conn.property:
		return False  # another hotel's thread id: never moved across properties

	reservation = existing.reservation if existing else None
	if booking_id and (not reservation or booking_id != existing.channex_booking_id):
		reservation = _reservation_for(conn.name, booking_id) or reservation
	old_count = cint(existing.message_count) if existing else 0
	notified = cint(existing.notified_count) if existing else 0
	no_reply = cint(existing.no_reply_needed) if existing else 0
	if count > old_count and sender == "guest":
		no_reply = 0  # the guest wrote again
	rang = False
	if quiet:
		notified = count
	elif count > notified:
		if sender == "guest":
			from kamra.notifications import record
			record(conn.property, "front-desk",
			       _("New message from {0}: {1}").format(platform(attrs.get("provider")),
			                                             _guest_name(reservation, attrs.get("provider"))),
			       doctype=DT, name=tid, route=f"/ota-messages?thread={tid}")
			rang = True
		notified = count
	values = {
		"connection": conn.name, "provider": str(attrs.get("provider") or ""),
		"channex_booking_id": booking_id, "reservation": reservation, "message_count": count,
		"last_message_at": _latest(attrs.get("last_message_received_at"), last.get("inserted_at")),
		"last_sender": sender, "is_closed": cint(bool(attrs.get("is_closed"))),
		"no_reply_needed": no_reply, "notified_count": notified,
	}
	if not existing:
		frappe.get_doc(dict(values, doctype=DT, thread_id=tid, property=conn.property)).insert(
			ignore_permissions=True)
	elif any(str(existing.get(k) or "") != str(v or "") for k, v in values.items()
	         if k in existing):
		frappe.db.set_value(DT, tid, values)
	return rang


def pull_messages(connection: str) -> dict:
	"""Worker (short queue): refresh the connection's thread index from
	Channex's thread list, every page up to MAX_PAGES. {"threads", "rang"};
	{"skipped": ...} while paused (no messages app: an hour; a 429: its
	Retry-After)."""
	from kamra.channels import channex
	conn = frappe.get_doc("Channel Manager Connection", connection)
	if conn.provider != "Channex" or not conn.active:
		return {"skipped": "inactive"}
	if not conn.external_property_id:
		return {"skipped": "no_property_id"}  # the account's threads, not this hotel's
	paused = frappe.cache.get_value(_OFF.format(connection=conn.name))
	if paused:
		return {"skipped": str(paused)}

	quiet = not frappe.db.exists(DT, {"connection": conn.name})
	out = {"threads": 0, "rang": 0}
	try:
		for page in range(1, MAX_PAGES + 1):
			rows, meta = channex.fetch_message_threads(conn, page=page)
			for item in rows:
				try:
					out["rang"] += _sync_thread(conn, item, quiet)
					out["threads"] += 1
				except Exception as exc:
					_log_error(conn, exc, str(item.get("id") or ""))
			limit = cint(meta.get("limit")) or channex.MESSAGE_PAGE_LIMIT
			if not rows or page * limit >= cint(meta.get("total")):
				break
	except channex.ChannexMessagingOff:
		_pause(conn.name, "messaging_off", 3600)
		out = {"skipped": "messaging_off"}
	except channex.ChannexRateLimited as exc:
		_pause(conn.name, "rate_limited", exc.retry_after)
		out = {"paused": exc.retry_after}
	except Exception as exc:
		_log_error(conn, exc)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- background worker: the index pass commits once, at its end
	return out


# ---------------------------------------------------------------------------
# The desk
# ---------------------------------------------------------------------------


def _connection(property: str, required: bool = False):
	name = frappe.db.get_value("Channel Manager Connection",
	                           {"property": property, "provider": "Channex", "active": 1},
	                           "name", order_by="creation asc")
	if not name and required:
		frappe.throw(_("This property has no active Channex connection."))
	return frappe.get_doc("Channel Manager Connection", name) if name else None


def _thread_row(property: str, thread: str):
	row = frappe.db.get_value(DT, thread, _FIELDS + ["property"], as_dict=True) if thread else None
	if not row or row.property != property:
		frappe.throw(_("Conversation not found."), frappe.DoesNotExistError)
	return row


def _is_admin() -> bool:
	return frappe.session.user == "Administrator" or bool(
		{"Hotel Admin", "System Manager"} & set(frappe.get_roles()))


def _preview(item: dict) -> str:
	last = (item.get("attributes") or {}).get("last_message") or {}
	text = " ".join(str(last.get("message") or "").split()) if isinstance(last, dict) else ""
	return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS - 1].rstrip() + "…"


def _live(conn) -> dict:
	"""Page 1 of Channex's thread list, for the previews (never stored, kept
	20 s in the cache): {"off": the messages app is missing, "previews"}."""
	from kamra.channels import channex
	key = _LIVE.format(connection=conn.name)
	cached = frappe.cache.get_value(key)
	if cached is not None:
		return cached
	try:
		rows, _meta = channex.fetch_message_threads(conn, page=1)
	except channex.ChannexMessagingOff:
		live = {"off": True, "previews": {}}
		_pause(conn.name, "messaging_off", 3600)
	except channex.ChannexRateLimited:
		# shares the key with the rates push: no refresh calls Channex again for 20 s
		live = {"off": False, "previews": {}}
	except Exception:
		return {"off": False, "previews": {}}  # the index still lists; no previews this time
	else:
		live = {"off": False, "previews": {str(r.get("id")): _preview(r) for r in rows}}
		if frappe.cache.get_value(_OFF.format(connection=conn.name)) == "messaging_off":
			# the app was installed meanwhile: the index catches up now
			frappe.cache.delete_value(_OFF.format(connection=conn.name))
			enqueue_messages_pull(conn.name)
	frappe.cache.set_value(key, live, expires_in_sec=20)
	return live


def _rows_out(rows: list, previews: dict) -> list[dict]:
	names = {r.reservation for r in rows if r.reservation}
	stays = {s.name: s for s in frappe.get_all("Reservation", filters={"name": ("in", list(names))},
		fields=["name", "guest_name", "check_in_date", "check_out_date", "nights", "room_type"])} \
		if names else {}
	types = {t.name: t.room_type_name for t in frappe.get_all("Room Type", filters={
		"name": ("in", list({s.room_type for s in stays.values() if s.room_type}))},
		fields=["name", "room_type_name"])} if stays else {}
	out = []
	for r in rows:
		stay = stays.get(r.reservation)
		out.append({
			"thread": r.name, "provider": r.provider, "platform": platform(r.provider),
			"reservation": r.reservation if stay else None,
			"guest_name": (stay.guest_name if stay else None) or _("Guest from {0}").format(platform(r.provider)),
			"check_in": str(stay.check_in_date) if stay and stay.check_in_date else None,
			"check_out": str(stay.check_out_date) if stay and stay.check_out_date else None,
			"nights": cint(stay.nights) if stay else None,
			"room_type": types.get(stay.room_type) if stay else None,
			"last_at": str(r.last_message_at) if r.last_message_at else None,
			"last_sender": r.last_sender or None,
			"unanswered": r.last_sender == "guest" and not cint(r.no_reply_needed),
			"no_reply_needed": bool(cint(r.no_reply_needed)),
			"is_closed": bool(cint(r.is_closed)),
			"preview": previews.get(r.name, ""),
		})
	return out


def _channex_failure(exc: Exception, provider: str | None = None, sending: bool = False):
	from kamra.channels import channex
	if isinstance(exc, channex.ChannexRateLimited):
		frappe.throw(_("Channex is busy; try again in a minute."))
	detail = getattr(exc, "detail", "") or str(getattr(exc, "status", "")) or type(exc).__name__
	if sending:
		frappe.throw(_("{0} did not accept the message: {1}").format(platform(provider), detail[:200]))
	frappe.throw(_("Could not read the conversation from Channex: {0}").format(detail[:200]))


@frappe.whitelist()
@require_perm(DT, "reply_messages", scope=_SCOPE)
def threads(property: str, only_unanswered: int = 0):
	"""The property's OTA conversations: unanswered first, then the latest.
	{"app_missing": Channex's messages app is not installed, "threads"}."""
	admin = _is_admin()
	conn = _connection(property)
	if not conn:
		return {"app_missing": False, "connected": False, "is_admin": admin, "threads": []}
	live = _live(conn)
	if live["off"]:
		return {"app_missing": True, "connected": True, "is_admin": admin, "threads": []}
	rows = frappe.get_all(DT, filters={"property": property}, fields=_FIELDS,
	                      order_by="last_message_at desc", limit=500)
	out = _rows_out(rows, live["previews"])
	if cint(only_unanswered):
		out = [r for r in out if r["unanswered"]]
	out.sort(key=lambda r: not r["unanswered"])  # stable: the latest first within each group
	return {"app_missing": False, "connected": True, "is_admin": admin, "threads": out}


@frappe.whitelist()
@require_perm(DT, "reply_messages", scope=_SCOPE)
def thread(property: str, thread: str):
	"""One conversation, read live from Channex, oldest message first."""
	from kamra.channels import channex
	row = _thread_row(property, thread)
	conn = _connection(property, required=True)
	head = _rows_out([row], {})[0]
	try:
		items = channex.fetch_thread_messages(conn, row.name)
	except channex.ChannexMessagingOff:
		return {"thread": head, "messages": [], "app_missing": True}
	except channex.ChannexError as exc:
		_channex_failure(exc)
	messages = []
	for item in items:
		attrs = item.get("attributes") or {}
		sender = attrs.get("sender") if attrs.get("sender") in SENDERS else "system"
		at = _local(attrs.get("inserted_at"))
		messages.append({
			"id": str(item.get("id") or ""), "sender": sender,
			"text": str(attrs.get("message") or ""), "at": str(at) if at else None,
			"attachments": [{"index": i} for i in range(len(attrs.get("attachments") or []))],
		})
	return {"thread": head, "messages": messages, "app_missing": False}


@frappe.whitelist(methods=["POST"])
@require_perm(DT, "reply_messages", scope=_SCOPE)
def reply(property: str, thread: str, text: str):
	"""Answer the guest: the text goes to the thread in Channex, and from
	there to the guest on their platform."""
	from kamra.channels import channex
	text = (text or "").strip()
	if not text:
		frappe.throw(_("Nothing to send."))
	row = _thread_row(property, thread)
	conn = _connection(property, required=True)
	try:
		created = channex.send_thread_message(conn, row.name, text)
	except channex.ChannexError as exc:
		_channex_failure(exc, row.provider, sending=True)
	count = cint(row.message_count) + 1
	at = _local(created.get("inserted_at")) or now_datetime()
	frappe.db.set_value(DT, row.name, {"last_sender": "property", "message_count": count,
		"notified_count": count, "no_reply_needed": 0, "last_message_at": at})
	frappe.cache.delete_value(_LIVE.format(connection=conn.name))
	return {"id": str(created.get("id") or ""), "sender": "property",
	        "text": str(created.get("message") or text), "at": str(at), "attachments": []}


@frappe.whitelist(methods=["POST"])
@require_perm(DT, "reply_messages", scope=_SCOPE)
def no_reply_needed(property: str, thread: str):
	"""Booking.com only: the guest's last message needs no answer (a
	"thanks"), so Booking does not count it as unanswered."""
	from kamra.channels import channex
	row = _thread_row(property, thread)
	if row.provider != "BookingCom":
		frappe.throw(_("Only Booking.com conversations can be marked as not needing a reply."))
	conn = _connection(property, required=True)
	try:
		channex.mark_no_reply_needed(conn, row.name)
	except channex.ChannexError as exc:
		_channex_failure(exc, row.provider, sending=True)
	frappe.db.set_value(DT, row.name, "no_reply_needed", 1)
	return {"ok": True}


def _attachment_path(att) -> str:
	if isinstance(att, dict):
		att = att.get("url") or att.get("file") or att.get("path") or ""
	return str(att or "")


@frappe.whitelist()
@require_perm(DT, "reply_messages", scope=_SCOPE)
def attachment(property: str, thread: str, message: str, index: int = 0):
	"""A file the guest sent, fetched from Channex and handed over (images
	shown, anything else downloaded). The path is read from the message in
	Channex, never taken from the browser."""
	from kamra.channels import channex
	row = _thread_row(property, thread)
	conn = _connection(property, required=True)
	try:
		items = channex.fetch_thread_messages(conn, row.name)
	except channex.ChannexError as exc:
		_channex_failure(exc)
	item = next((i for i in items if str(i.get("id") or "") == str(message or "")), None)
	files = ((item or {}).get("attributes") or {}).get("attachments") or []
	idx = cint(index)
	if not item or not 0 <= idx < len(files):
		frappe.throw(_("Attachment not found."), frappe.DoesNotExistError)
	try:
		content, ctype = channex.fetch_attachment(conn, _attachment_path(files[idx]))
	except ValueError:
		frappe.throw(_("Attachment not found."), frappe.DoesNotExistError)
	except channex.ChannexError as exc:
		_channex_failure(exc)
	ctype = (ctype or "").lower()
	frappe.local.response.update({
		"type": "download",
		"filename": f"attachment-{idx + 1}{_EXTENSIONS.get(ctype, '')}",
		"filecontent": content,
		"content_type": ctype if ctype in _INLINE else "application/octet-stream",
		"display_content_as": "inline" if ctype in _INLINE else "attachment",
	})


@frappe.whitelist()
@require_perm(DT, "reply_messages", scope=_SCOPE)
def for_reservation(property: str, reservation: str):
	"""The OTA conversations of one reservation, latest first: the threads
	linked to it, and those of any Channex booking whose revision made it (a
	multi-room booking has one thread and several reservations)."""
	if frappe.db.get_value("Reservation", reservation, "property") != property:
		return []
	connections = frappe.get_all("Channel Manager Connection",
	                             filters={"property": property, "provider": "Channex"}, pluck="name")
	bookings = set()
	if connections:
		for rev in frappe.get_all("Channel Booking Revision", filters={
				"connection": ("in", connections), "status": ("in", ("Applied", "Acked")),
				"results": ("like", f"%{reservation}%")}, fields=["booking_id", "results"]):
			if rev.booking_id and reservation in _reservations_in(rev.results):
				bookings.add(rev.booking_id)
	rows = frappe.get_all(DT, filters={"property": property, "reservation": reservation},
	                      fields=_FIELDS, order_by="last_message_at desc")
	if bookings:
		seen = {r.name for r in rows}
		rows += [r for r in frappe.get_all(DT, filters={"property": property,
			"channex_booking_id": ("in", list(bookings))}, fields=_FIELDS) if r.name not in seen]
	rows.sort(key=lambda r: str(r.last_message_at or ""), reverse=True)
	return [{"thread": r.name, "provider": r.provider, "platform": platform(r.provider),
	         "unanswered": r.last_sender == "guest" and not cint(r.no_reply_needed),
	         "last_at": str(r.last_message_at) if r.last_message_at else None} for r in rows]
