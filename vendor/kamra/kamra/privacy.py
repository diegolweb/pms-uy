"""Guest privacy by design (India DPDP Act 2023 and Rules 2025; GDPR-friendly).

The hotel is the Data Fiduciary for its guests. Kamra's job is to make the
right thing the default and the lawful thing one click:

  erase_guest        right to erasure (s.12): every identifying trace goes,
                     including photos, signatures, message logs and change
                     history; bills and stays stay for the tax books.
  export_guest_data  right to access (s.11): a summary of what is held.
  apply_retention    storage limitation (s.8(7)): guests with no stay for
                     Property.guest_retention_months are erased by the daily
                     scheduler. Guests on the blacklist are kept (security is
                     the stated purpose) until                      the flag is lifted.
  apply_message_retention
                     storage limitation for conversations: WhatsApp message
                     text and AI assistant chats older than
                     Property.message_retention_days are cleared nightly;
                     who, when and delivery status stay for audit.
  guest_traceback    the Error Log: an error about a guest's record is
                     logged without the frames' local variables and without
                     the guest's name, email, phone or ID number.

What is deliberately kept after erasure: amounts, dates, rooms and invoice
numbers (GST law needs them), an ID masked to its last 4 digits in the stay
register, and one audit line saying the erasure happened (without the name).
"""


import re
import sys
import traceback
from collections import defaultdict

import frappe
from frappe.model.document import Document
from frappe.utils import add_days, add_months, cint, getdate, now_datetime, nowdate

from kamra.authz import require_roles


def _mask(value: str | None) -> str | None:
	if not value or len(value) <= 4 or value.startswith("•"):
		return value
	return "•" * (len(value) - 4) + value[-4:]


def _delete_files(doctype: str, name: str, field: str | None = None, url: str | None = None) -> int:
	filters = {"attached_to_doctype": doctype, "attached_to_name": name}
	if field:
		filters["attached_to_field"] = field
	if url:
		filters["file_url"] = url
	gone = 0
	for f in frappe.get_all("File", filters=filters, pluck="name"):
		# delete_doc handles shared content hashes; delete_permanently leaves
		# no Deleted Document row holding the file name
		frappe.delete_doc("File", f, force=True, ignore_permissions=True, delete_permanently=True)
		gone += 1
	return gone


def _scrub_text(doctype: str, fields: tuple, secrets: list[str], alias: str, where: str = "", values=()):
	"""Replace each identifying string with the alias in free-text columns."""
	for field in fields:
		for s in secrets:
			frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- doctype/field are constants from this module; values parameterized
				f"UPDATE `tab{doctype}` SET `{field}` = REPLACE(`{field}`, %s, %s) "
				f"WHERE `{field}` LIKE %s {where}",
				(s, alias, f"%{s}%", *values))


def erase_guest(guest: str, reason: str = "request") -> dict:
	"""Irreversibly remove what identifies a guest. Financial records remain."""
	g = frappe.get_doc("Guest", guest)
	alias = f"Guest {frappe.generate_hash(length=6).upper()}"
	secrets = [s for s in {g.full_name, g.phone, g.email, g.id_number,
	                       f"{g.first_name or ''} {g.last_name or ''}".strip()} if s and len(s) >= 4]
	reservations = frappe.get_all("Reservation", filters={"guest": guest}, pluck="name")
	counts = {"files": 0, "messages": 0, "occupants": 0}

	# 1. the profile itself, photos included
	for field in ("id_file", "address_proof_file", "photo"):
		counts["files"] += _delete_files("Guest", guest, field)
	counts["files"] += _delete_files("Guest", guest)
	blank = {"first_name": alias, "last_name": "", "full_name": alias, "phone": "", "email": "",
	         "id_type": "", "id_number": "", "id_place_of_issue": "", "id_issue_date": None,
	         "id_expiry_date": None, "nationality": "", "address_line": "", "city": "",
	         "id_file": None, "address_proof_file": None, "photo": None, "date_of_birth": None, "gender": "",
	         "guest_notes": "Profile erased.", "vip": 0, "marketing_consent": 0}
	frappe.db.set_value("Guest", guest, {k: v for k, v in blank.items() if g.meta.has_field(k)},
	                    update_modified=False)

	# 2. stays: names on the books become the alias; signatures, booker
	#    contact, occupant photos and phones go; IDs are masked
	for doctype in ("Reservation", "Folio"):
		frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- doctype is a constant from this loop
			f"UPDATE `tab{doctype}` SET guest_name = %s WHERE guest = %s", (alias, guest))
	for r in reservations:
		res = frappe.get_doc("Reservation", r)
		changes = {"booked_by_phone": "", "precheckin_signature": None}
		if res.get("booked_by_name") and res.booked_by_name in secrets:
			changes["booked_by_name"] = alias
		frappe.db.set_value("Reservation", r, {k: v for k, v in changes.items() if res.meta.has_field(k)},
		                    update_modified=False)
		for o in res.get("occupants") or []:
			oc = {"id_number": _mask(o.id_number), "phone": ""}
			if o.get("id_file"):
				counts["files"] += _delete_files("Reservation", r, url=o.id_file)
				oc["id_file"] = None
			if o.get("photo"):
				counts["files"] += _delete_files("Reservation", r, url=o.photo)
				oc["photo"] = None
			if o.full_name in secrets:
				oc["full_name"] = alias
			frappe.db.set_value("Stay Occupant", o.name, oc, update_modified=False)
			counts["occupants"] += 1
		from kamra.id_documents import discard_id_document
		discard_id_document(r)
		for dt in ("Laundry Order", "Service Ticket"):
			frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- dt is a constant from this loop
				f"UPDATE `tab{dt}` SET guest_name = %s WHERE reservation = %s", (alias, r))

	# 3. conversations about the guest
	for m in frappe.get_all("WhatsApp Message", filters={"guest": guest}, pluck="name"):
		frappe.db.set_value("WhatsApp Message", m, {"content": "[erased]", "to_number": "",
		                                            "from_number": "", "guest": None},
		                    update_modified=False)
		counts["messages"] += 1
	_scrub_text("Copilot Conversation", ("messages", "title"), secrets, alias)
	_scrub_text("Communication", ("content", "subject", "recipients", "sender", "phone_no"), secrets, alias)

	# 4. audit and change history keep what happened, not who it was
	_scrub_text("Agent Action Log", ("rationale", "before_snapshot", "after_snapshot"), secrets, alias)
	frappe.db.delete("Version", {"ref_doctype": "Guest", "docname": guest})
	_scrub_text("Version", ("data",), secrets, alias,
	            "AND ref_doctype IN ('Reservation', 'Folio')", ())

	from kamra.savings import log_action
	log_action("erase_guest", "Guest", guest,
	           rationale=f"Personal data erased ({reason}); financial records kept for the books")
	return {"guest": guest, "alias": alias, "reservations": len(reservations), **counts}


@frappe.whitelist(methods=["POST"])
@require_roles(scope={"guest": "Guest"})
def export_guest_data(guest: str) -> dict:
	"""Right to access: everything Kamra holds about one guest, readable."""
	g = frappe.get_doc("Guest", guest)
	profile = {f.fieldname: g.get(f.fieldname) for f in g.meta.fields
	           if f.fieldtype not in ("Section Break", "Column Break", "Tab Break") and g.get(f.fieldname)}
	stays = frappe.get_all("Reservation", filters={"guest": guest},
	                       fields=["name", "property", "check_in_date", "check_out_date", "status",
	                               "adults", "children", "source"], order_by="check_in_date desc")
	folios = frappe.get_all("Folio", filters={"guest": guest},
	                        fields=["name", "reservation", "status", "grand_total", "balance"])
	messages = frappe.get_all("WhatsApp Message", filters={"guest": guest},
	                          fields=["creation", "direction", "status", "template_name"],
	                          order_by="creation desc", limit=200)
	from kamra.savings import log_action
	log_action("export_guest_data", "Guest", guest, rationale="Data access request answered")
	return {"generated_on": str(frappe.utils.now_datetime()), "guest": guest, "profile": profile,
	        "stays": stays, "bills": folios, "messages": messages,
	        "note": "Bills and stay dates are kept as tax records even after erasure."}


def _last_activity(guest: str):
	row = frappe.db.sql("""select max(greatest(coalesce(check_out_date, '1970-01-01'),
		date(modified))) from `tabReservation` where guest=%s""", guest)
	last = row[0][0] if row and row[0][0] else None
	return getdate(last) if last else getdate(frappe.db.get_value("Guest", guest, "modified"))


def apply_retention(limit: int = 500) -> dict:
	"""Daily: erase guests idle past their property's retention period.
	A guest's period is the longest among the properties they stayed at;
	any property with retention off (0) keeps them."""
	months = {p.name: cint(p.guest_retention_months)
	          for p in frappe.get_all("Property", fields=["name", "guest_retention_months"])}
	if not any(months.values()):
		return {"erased": 0}
	erased = 0
	candidates = frappe.db.sql("""select g.name from `tabGuest` g
		where coalesce(g.blacklisted, 0) = 0 and g.full_name not like 'Guest %%'
		and not exists (select 1 from `tabReservation` r where r.guest = g.name
		                and r.status in ('Tentative', 'Pending Payment', 'Held', 'Confirmed', 'Checked In'))
		order by g.modified limit %s""", limit, pluck=True)
	for guest in candidates:
		props = set(frappe.get_all("Reservation", filters={"guest": guest}, pluck="property"))
		keep = [months.get(p, 0) for p in props] or [0]
		if 0 in keep:
			continue
		if _last_activity(guest) < getdate(add_months(nowdate(), -max(keep))):
			erase_guest(guest, reason="retention period ended")
			erased += 1
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- scheduler: one guest's erasure must not roll back another's
	return {"erased": erased}


CLEARED_CHAT_TITLE = "Chat cleared (retention period ended)"


def apply_message_retention() -> dict:
	"""Daily: clear WhatsApp message text and AI assistant chats older than
	their property's message_retention_days. Rows stay, so delivery status,
	direction, template and timestamps remain for audit. A chat with no
	property follows the shortest period set on any property."""
	days = {p.name: cint(p.message_retention_days)
	        for p in frappe.get_all("Property", fields=["name", "message_retention_days"])}
	set_days = [d for d in days.values() if d > 0]
	if not set_days:
		return {"messages": 0, "chats": 0}
	now = now_datetime()
	messages = chats = 0
	for prop, d in days.items():
		if d <= 0:
			continue
		cutoff = add_days(now, -d)
		names = frappe.get_all("WhatsApp Message", pluck="name", filters={
			"property": prop, "creation": ("<", cutoff), "content": ("not in", ("", "[erased]"))})
		for name in names:
			frappe.db.set_value("WhatsApp Message", name, "content", "", update_modified=False)
		messages += len(names)
		chats += _clear_chats({"property": prop, "modified": ("<", cutoff)})
	chats += _clear_chats({"property": ("is", "not set"),
	                       "modified": ("<", add_days(now, -min(set_days)))})
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- scheduler: persist the nightly clean-up
	return {"messages": messages, "chats": chats}


def _clear_chats(filters: dict) -> int:
	names = frappe.get_all("Copilot Conversation", pluck="name",
	                       filters={**filters, "messages": ("!=", "[]")})
	for name in names:
		# modified is the last activity; keep it so the chat is not
		# picked up again as fresh
		frappe.db.set_value("Copilot Conversation", name,
		                    {"messages": "[]", "title": CLEARED_CHAT_TITLE}, update_modified=False)
	return len(names)


# Marketing consent (DPDP s.6): promotional WhatsApp goes only to guests who
# opted in, and withdrawing is as easy as giving it.
CONSENT_CHANNEL = {"Booking Page": "API", "Desk": "Desk", "WhatsApp Keyword": "WhatsApp"}


def set_marketing_consent(guest: str, consent, source: str, property: str | None = None) -> bool:
	"""The only writer of Guest.marketing_consent. Records when and how, and
	logs the change. Returns False when nothing changed."""
	if source not in CONSENT_CHANNEL:
		frappe.throw(f"Unknown consent source {source}.")
	consent = 1 if cint(consent) else 0
	if cint(frappe.db.get_value("Guest", guest, "marketing_consent")) == consent:
		return False
	frappe.db.set_value("Guest", guest, {"marketing_consent": consent,
	                                     "marketing_consent_date": now_datetime(),
	                                     "marketing_consent_source": source})
	from kamra.savings import log_action
	verb = "granted" if consent else "withdrawn"
	log_action(f"marketing_consent_{verb}", "Guest", guest, property=property,
	           channel=CONSENT_CHANNEL[source],
	           rationale=f"Marketing consent {verb} ({source})")
	return True


@frappe.whitelist(methods=["POST"])
@require_roles("Front Desk", "Kamra Agent", scope={"guest": "Guest"})
def update_marketing_consent(guest: str, consent) -> dict:
	"""Desk toggle in the guest profile: record or withdraw consent."""
	set_marketing_consent(guest, consent, "Desk")
	return frappe.db.get_value("Guest", guest, ["marketing_consent", "marketing_consent_date",
	                                            "marketing_consent_source"], as_dict=True)


# ---------------------------------------------------------------------------
# the Error Log
# ---------------------------------------------------------------------------
# frappe.log_error with no message stores the traceback with every frame's
# local variables on a developer-mode site (the booking, the guest, the
# payload), and on any site the error text itself may quote a guest (an
# address the mail server refused, a phone a provider rejected). Every
# log_error in Kamra passes a message - a test reads the source to keep it
# so - and where the error can be about a guest that message is
# guest_traceback(<the record>).

_PHONE_TAIL = 7  # a phone is found by its last digits, whatever the prefix or the separators
_PHONE_SEP = r"[\s\-.()/]*"


def _phone_pattern(phone) -> re.Pattern | None:
	"""Any writing of the number that ends in its last digits: +598 99 123
	456, +59899123456, 099 123 456..."""
	digits = re.sub(r"\D", "", str(phone or ""))
	if len(digits) < 4:
		return None
	tail = _PHONE_SEP.join(digits[-_PHONE_TAIL:])
	return re.compile(r"\+?(?:\d" + _PHONE_SEP + r")*?" + tail + r"(?!\d)")


def _scrub_guest(text: str, events: list[dict]) -> str:
	"""An error text with the guests taken out: the name (whole and each
	word), the email (whole and its local part), the phone in any writing,
	the ID number and the notes. `events` are dicts with any of guest_name,
	email, phone, id_number and notes - a channel delivery's events, or
	guests_of(...)."""
	secrets: set[str] = set()
	patterns = []
	for e in events or []:
		name = str(e.get("guest_name") or "")
		if name != "OTA Guest":  # the parser's placeholder, nobody's name
			secrets.add(name)
			secrets.update(name.split())
		email = str(e.get("email") or "")
		secrets.update((email, email.split("@")[0]))
		secrets.add(str(e.get("id_number") or ""))
		secrets.add(str(e.get("notes") or ""))
		if pattern := _phone_pattern(e.get("phone")):
			patterns.append(pattern)
	for pattern in patterns:
		text = pattern.sub("[redacted]", text)
	for secret in sorted((s.strip() for s in secrets if len(s.strip()) > 2), key=len, reverse=True):
		text = re.sub(re.escape(secret), "[redacted]", text, flags=re.IGNORECASE)
	return text


# who a record names: (name, email, phone, ID number) fields, per doctype
_PEOPLE = {
	"Guest": (("full_name", "email", "phone", "id_number"), ("first_name", None, None, None),
	          ("last_name", None, None, None)),
	"Reservation": (("guest_name", None, None, None), ("booked_by_name", None, "booked_by_phone", None)),
	"Folio": (("guest_name", None, None, None), ("bill_to_name", None, None, None)),
	"Stay Occupant": (("full_name", None, "phone", "id_number"),),
	"Venue Booking": (("customer_name", "customer_email", "customer_phone", None),
	                  ("billing_name", None, None, None)),
}
_NOTES = {"Reservation": "special_requests"}
# the links that lead from a record to the people behind it
_LINKS = ("guest", "reservation", "folio", "master_reservation")


def _people(doctype: str, row) -> list[dict]:
	out = []
	for name, email, phone, id_number in _PEOPLE.get(doctype, ()):
		person = {"guest_name": row.get(name), "email": email and row.get(email),
		          "phone": phone and row.get(phone), "id_number": id_number and row.get(id_number)}
		if any(person.values()):
			out.append(person)
	if (notes := _NOTES.get(doctype)) and row.get(notes):
		out.append({"notes": row.get(notes)})
	return out


def _follow(meta, row, wanted: dict) -> None:
	for fieldname in _LINKS:
		field = meta.get_field(fieldname)
		if field and field.fieldtype == "Link" and row.get(fieldname):
			wanted[field.options].add(row.get(fieldname))


def _stays_on(property: str, day=None) -> list[str]:
	"""The stays a property has on `day` (its business date by default): in
	house, arriving and leaving."""
	day = day or frappe.db.get_value("Property", property, "business_date") or nowdate()
	return frappe.get_all("Reservation", filters={"property": property,
		"check_in_date": ("<=", day), "check_out_date": (">=", day)}, pluck="name")


def guests_of(*records, stays: tuple | None = None) -> list[dict]:
	"""The people `records` name, in _scrub_guest's shape: a Reservation's
	guest, booker and occupants; a Folio's guest and its stay's; a Guest; a
	Venue Booking's customer; any other record through its guest,
	reservation, folio or master_reservation link. A record is a Document
	(read as it stands, saved or not) or (doctype, name or list of names);
	None is skipped. stays=(property, day) adds every stay the property has
	that day (day None: its business date), for a job that runs a hotel."""
	people: list[dict] = []
	wanted: dict[str, set] = defaultdict(set)
	for record in records:
		if record is None:
			continue
		if isinstance(record, Document):
			people += _people(record.doctype, record)
			for table in record.meta.get_table_fields():
				for row in record.get(table.fieldname) or []:
					people += _people(table.options, row)
			_follow(record.meta, record, wanted)
			continue
		doctype, names = record
		wanted[doctype].update([names] if isinstance(names, str) else (names or []))
	if stays:
		wanted["Reservation"].update(_stays_on(*stays))
	done: dict[str, set] = defaultdict(set)
	for _ in range(4):  # a link leads down (a POS order -> folio -> stay -> guest): a few hops
		batch = {dt: {n for n in names if n} - done[dt] for dt, names in list(wanted.items())}
		batch = {dt: names for dt, names in batch.items() if names}
		if not batch:
			break
		for doctype, names in batch.items():
			done[doctype] |= names
			meta = frappe.get_meta(doctype)
			fields = {f for spec in _PEOPLE.get(doctype, ()) for f in spec if f}
			fields |= {f for f in (_NOTES.get(doctype), *_LINKS) if f}
			fields = ["name", *sorted(f for f in fields if meta.has_field(f))]
			for row in frappe.get_all(doctype, filters={"name": ("in", sorted(names))}, fields=fields):
				people += _people(doctype, row)
				_follow(meta, row, wanted)
			for table in meta.get_table_fields():
				if table.options not in _PEOPLE:
					continue
				child = frappe.get_meta(table.options)
				child_fields = [f for spec in _PEOPLE[table.options] for f in spec
				                if f and child.has_field(f)]
				for row in frappe.get_all(table.options, fields=child_fields, filters={
						"parenttype": doctype, "parentfield": table.fieldname,
						"parent": ("in", sorted(names))}):
					people += _people(table.options, row)
	return people


def scrub_people(text: str, *records, stays: tuple | None = None, withheld: str = "") -> str:
	"""`text` with the people of `records` (see guests_of) taken out, or
	`withheld` if they cannot be looked up: an error path never fails, and
	never keeps a guest it could not find."""
	try:
		return _scrub_guest(text, guests_of(*records, stays=stays))
	except Exception:
		return withheld


def withheld_traceback(exc: BaseException | None) -> str:
	"""exc's traceback with every exception's text left out: the frames, the
	code and the exception types stay, the chained exceptions too. Never
	raises (it is the last resort of an error path)."""
	try:
		return _withheld(exc)
	except Exception:
		return f"{type(exc).__name__}: [withheld: its text may name a guest]"


def _withheld(exc: BaseException | None) -> str:
	from frappe.utils import get_bench_path
	parts, seen = [], set()
	while exc is not None and id(exc) not in seen:
		seen.add(id(exc))
		kind = type(exc)
		parts.append("Traceback (most recent call last):\n"
		             + "".join(traceback.format_tb(exc.__traceback__))
		             + f"{kind.__module__}.{kind.__qualname__}: [withheld: its text may name a guest]")
		if exc.__cause__ is not None:
			parts.append("\nThe above exception was the direct cause of the following exception:\n")
			exc = exc.__cause__
		elif exc.__context__ is not None and not exc.__suppress_context__:
			parts.append("\nDuring handling of the above exception, another exception occurred:\n")
			exc = exc.__context__
		else:
			exc = None
	return "\n".join(reversed(parts)).replace(get_bench_path() + "/", "")


def guest_traceback(*records, stays: tuple | None = None) -> str:
	"""The message for frappe.log_error in an `except` that may be about a
	guest: the traceback without local variables and without the people of
	`records` (see guests_of). If they cannot be looked up, the error texts
	are withheld (withheld_traceback): the trace stays, the guest never
	goes in."""
	exc = sys.exc_info()[1]
	text = frappe.get_traceback(with_context=False)
	try:
		return _scrub_guest(text, guests_of(*records, stays=stays))
	except Exception:
		return withheld_traceback(exc)
