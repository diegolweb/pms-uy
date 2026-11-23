"""Localization seam. The core PMS never knows about GST, VAT or fiscal
printers - it asks the country pack. Packs are resolved through the
`kamra_localization` hook (ERPNext regional_overrides style), so a future
`kamra_uae` app claims its country just by declaring the hook. Countries
without a pack fall back to a plain flat-tax `generic` pack.

Interface every pack implements (see india/):
  calculate_room_tax(property, room_type_doc, nightly_rate) -> Decimal
  fnb_tax_rate(property) -> float
  tax_rate_options(property) -> list[float]
  invoice_context(prop_doc) -> dict   (labels, service code, place of supply)
  locale(prop_doc) -> dict            (currency_symbol, locale, tax_label...)

Optional, for invoice printing - a pack that doesn't implement these gets
a sensible default from the accessors at the bottom of this file, so an
existing pack keeps working untouched:
  service_code_for(prop_doc, charge_type) -> dict | None   (per-LINE SAC/HSN)
  tax_split(prop_doc, buyer_tax_id) -> list[(label, share)]
  amount_in_words(prop_doc, amount) -> str

Optional country integrations - absent means "not offered in this country",
and the accessors refuse rather than fall back to another country's:
  PAYMENT_GATEWAYS = [...]; payment_provider(gateway) -> module
  tax_return_rows(property, from_date, to_date) -> list[dict]
  verify_tax_id(property, tax_id, business_name) -> dict
  verify_guest_id(property, guest, id_type, consent) -> dict
"""

import importlib
import inspect
from datetime import date

import frappe


def default_country() -> str:
	"""The install's own country (System Settings, set by the setup wizard),
	or "" - never a guess."""
	return (frappe.db.get_single_value("System Settings", "country") or "").strip()


def property_country(property: str | None) -> str:
	"""The property's country, else the install's, else ""."""
	country = ""
	if property:
		country = (frappe.get_cached_value("Property", property, "country") or "").strip()
	return country or default_country()


def pack_for(property: str | None = None):
	"""The country pack for a property. A property with no country takes the
	install's; with neither, the plain generic pack - never India by guess."""
	return pack_for_country(property_country(property))


def pack_for_country(country: str):
	mapping = frappe.get_hooks("kamra_localization") or {}
	target = mapping.get(country)
	if target:
		path = target[-1] if isinstance(target, (list, tuple)) else target
		try:
			return importlib.import_module(path)
		except ModuleNotFoundError:
			pass
	from kamra.localization import generic
	return generic


# ── optional pack behaviour, with defaults ───────────────────────────────
#
#   services_tax_rate(property) -> float   (laundry & other guest services)
# A pack that predates these keeps working: each accessor falls back to
# something correct-but-plain, so adding a country never means editing the
# invoice printer.


def services_tax_rate(pack, property: str | None) -> float:
	"""The tax rate on a guest service such as laundry. A pack can say so
	(India: 18% GST on services); otherwise the pack's standard rate - the
	one its F&B rate already resolves to in every flat-VAT country."""
	fn = getattr(pack, "services_tax_rate", None)
	if fn:
		return float(fn(property))
	return float(pack.fnb_tax_rate(property))


def store_id_number(pack, id_type: str | None, value: str | None) -> str | None:
	"""How a guest's ID number is kept. Core treats every ID as a plain card
	and keeps what was typed; a pack can hold a national ID differently
	(India: Aadhaar keeps only its last 4 digits). None means no rule."""
	fn = getattr(pack, "store_id_number", None)
	return fn(id_type, value) if fn else None


def read_id_scan(pack, text: str) -> dict | None:
	"""Guest fields from a national ID's own code (India: the Aadhaar QR).
	Core reads passport MRZ itself; None means the pack has no reader or
	the scan is not one it knows."""
	fn = getattr(pack, "read_id_scan", None)
	return fn(text) if fn else None


def home_country_iso3(pack) -> str | None:
	"""The 3-letter code a passport MRZ uses for the pack's own country."""
	return getattr(pack, "HOME_COUNTRY_ISO3", None)


def default_nationality(pack) -> str:
	return getattr(pack, "DEFAULT_NATIONALITY", "") or ""


def money_text(property: str | None, amount) -> str:
	"""An amount in the property's currency for server-written text (the
	activity log, alerts) - never a hardcoded rupee sign."""
	symbol, currency = "", ""
	try:
		if property:
			prop = frappe.get_cached_doc("Property", property)
			loc = pack_for(property).locale(prop)
			symbol, currency = loc.get("currency_symbol") or "", loc.get("currency") or ""
	except Exception:
		symbol = ""
	try:
		value = float(amount or 0)
	except (TypeError, ValueError):
		return f"{symbol}{amount}"
	return f"{'-' if value < 0 else ''}{symbol}{group_digits(abs(value), currency == 'INR')}"


def group_digits(value: float, lakh: bool = False) -> str:
	"""11025 -> 11,025 and 28360.4 -> 28,360.40; with lakh, 120000 -> 1,20,000
	as the desk shows rupees."""
	text = f"{value:,.2f}".removesuffix(".00")
	if not lakh:
		return text
	whole, _, frac = text.replace(",", "").partition(".")
	head, tail = whole[:-3], whole[-3:]
	groups = [head[max(i - 2, 0):i] for i in range(len(head), 0, -2)][::-1]
	return ",".join([*groups, tail]) + (f".{frac}" if frac else "")


def service_code_for(pack, prop_doc, charge_type: str | None = None):
	"""The tax service code for ONE line. A bill that mixes a room night,
	a restaurant cover and a laundry bag carries three different codes -
	printing the accommodation code against all of them is wrong."""
	fn = getattr(pack, "service_code_for", None)
	if fn:
		return fn(prop_doc, charge_type)
	return pack.invoice_context(prop_doc).get("service_code")


def tax_split(pack, prop_doc, buyer_tax_id: str | None = None):
	"""How the tax on this bill is named and divided - [(label, share)].
	Passed the buyer's tax id because in some countries who they are (and
	where) changes the answer."""
	fn = getattr(pack, "tax_split", None)
	if fn:
		return fn(prop_doc, buyer_tax_id)
	return pack.invoice_context(prop_doc)["split"]


def invoice_tax_note(pack) -> str:
	"""A line under the invoice on how tax is shown (India: GST inclusive),
	or "" where the pack has none."""
	return getattr(pack, "INVOICE_TAX_NOTE", None) or ""


def invoice_language(prop_doc) -> str:
	"""The language a bill is written in: the property's booking_language,
	else the site's language, else "en". Base code only ("pt-BR" -> "pt")."""
	language = (prop_doc.get("booking_language") or "").strip()
	if not language:
		language = (frappe.db.get_single_value("System Settings", "language") or "").strip()
	return (language.replace("_", "-").split("-")[0] or "en").lower()


def amount_in_words(pack, prop_doc, amount) -> str:
	"""The invoice's amount in words. The pack's own speller if it has one;
	else num2words in the bill's language; else Kamra's English speller."""
	fn = getattr(pack, "amount_in_words", None)
	if fn:
		return fn(prop_doc, amount)
	from kamra.localization.words import amount_in_words as spell
	from kamra.localization.words import amount_in_words_in

	currency = pack.locale(prop_doc).get("currency") or ""
	return (amount_in_words_in(amount, currency, invoice_language(prop_doc))
	        or spell(amount, currency, indian=False))


# ── front-desk vocabulary: IDs and ways to pay ───────────────────────────
# A pack declares ID_TYPES / PAYMENT_MODES / DEFAULT_* as module constants.
# The desk offers Cash, Card and Bank Transfer everywhere. A pack may add
# its own instant / wallet modes (INSTANT_PAYMENT_MODES, e.g. India's UPI):
# each must also be a Folio Payment / Cashier Transaction option, and the
# till keeps them in their own bucket (Cashier Session.system_upi).

GENERIC_ID_TYPES = ["Passport", "National ID", "Driving License", "Other"]
GENERIC_PAYMENT_MODES = ["Cash", "Card", "Bank Transfer"]
# modes that are not a country's: any property may record them
CORE_PAYMENT_MODES = ("Cash", "Foreign Cash", "Card", "Bank Transfer", "OTA Prepaid",
                      "Company Credit", "Payment Link", "Cheque", "Other")


def id_types(pack) -> list[str]:
	return list(getattr(pack, "ID_TYPES", None) or GENERIC_ID_TYPES)


def instant_payment_modes(pack) -> list[str]:
	return list(getattr(pack, "INSTANT_PAYMENT_MODES", None) or [])


def payment_modes(pack) -> list[str]:
	modes = getattr(pack, "PAYMENT_MODES", None) or GENERIC_PAYMENT_MODES
	allowed = (*GENERIC_PAYMENT_MODES, *instant_payment_modes(pack))
	return [m for m in modes if m in allowed]


def cash_denominations(pack) -> list[float]:
	"""The notes and coins a till counts at close (CASH_DENOMINATIONS),
	largest first. Empty where the pack names none: any value is counted."""
	return [float(v) for v in getattr(pack, "CASH_DENOMINATIONS", None) or ()]


def outlet_payment_modes(pack) -> list[str]:
	"""What a bill is settled with at an outlet (POS): cash, card and the
	pack's instant modes."""
	instant = instant_payment_modes(pack)
	return [m for m in payment_modes(pack) if m in ("Cash", "Card") or m in instant]


def assert_payment_mode(property: str, mode: str | None) -> None:
	"""Refuse a country's own mode (UPI and alike) where the property's
	pack does not offer it. The shared money writers call this."""
	if not mode or mode in CORE_PAYMENT_MODES:
		return
	modes = payment_modes(pack_for(property))
	if mode not in modes:
		frappe.throw(frappe._("{0} is not a payment mode here. Pick one of: {1}.")
		             .format(mode, ", ".join(modes)))


def default_currency(pack) -> str:
	"""The pack's home currency, for a property that has none set."""
	return getattr(pack, "DEFAULT_CURRENCY", None) or "USD"


def front_desk_vocabulary(pack) -> dict:
	return {
		"id_types": id_types(pack),
		# a help line per ID type, e.g. which copy of a national ID is fine
		"id_type_hints": dict(getattr(pack, "ID_TYPE_HINTS", None) or {}),
		"payment_modes": payment_modes(pack),
		"instant_payment_modes": instant_payment_modes(pack),
		"outlet_payment_modes": outlet_payment_modes(pack),
		"default_nationality": getattr(pack, "DEFAULT_NATIONALITY", "") or "",
		"adult_age": adult_age(pack),
		"minor_ids": minor_ids(pack),
		"asks_residence": asks_residence(pack),
	}


# ── where the guest lives, for tax ───────────────────────────────────────
#
# Some countries tax a stay by where the guest LIVES, not by nationality:
# in Uruguay a non-resident's room nights carry no VAT, a resident's do in
# high season. A pack like that asks the desk, and keeps the answer itself
# (its own fields). It implements all five:
#
#   guest_residence(reservation) -> dict | None   what the desk sees:
#       {"home_country": "Uruguay",
#        "declared": "resident" | "non_resident" | None,   said at booking
#        "country": "<Country>" | None,    the guest's country of residence
#        "can_declare": bool,              still before check-in
#        "missing": bool}                  check-in cannot go on without it
#   declare_residence(reservation, resident: bool)    an existing booking
#   residence_on_new_booking(doc, resident: bool)     a booking being made
#   quote_residence(quote, resident: bool, inputs) -> quote    (get_quote)
#   save_residence_country(guest, country)            at check-in
#
# A pack without them never asks: the desk screens show nothing.

def asks_residence(pack) -> bool:
	return callable(getattr(pack, "guest_residence", None))


def guest_residence(pack, reservation: str):
	return pack.guest_residence(reservation) if asks_residence(pack) else None


def _resident(value) -> bool | None:
	""""1" / "0" (or a bool) from the desk; anything else is no answer."""
	if isinstance(value, bool):
		return value
	value = str(value if value is not None else "").strip()
	return {"1": True, "0": False}.get(value)


def declare_residence(pack, reservation: str, tax_resident) -> None:
	resident = _resident(tax_resident)
	if not asks_residence(pack) or resident is None:
		frappe.throw(frappe._("This property does not ask where the guest lives."))
	pack.declare_residence(reservation, resident)


def residence_on_new_booking(pack, doc, tax_resident) -> None:
	resident = _resident(tax_resident)
	if asks_residence(pack) and resident is not None:
		pack.residence_on_new_booking(doc, resident)


def quote_residence(pack, quote, tax_resident, inputs: dict):
	resident = _resident(tax_resident)
	if not asks_residence(pack) or resident is None:
		return quote
	return pack.quote_residence(quote, resident, inputs)


def save_residence_country(pack, guest: str, country: str) -> None:
	if asks_residence(pack) and (country or "").strip():
		pack.save_residence_country(guest, country)


def financial_year_start(pack, d) -> date:
	"""The first day of the financial year d falls in, from the pack's
	FINANCIAL_YEAR_START (month, day): 1 April 2026 for 10 Feb 2027 where
	the year runs from 1 April (India); 1 January where it has none."""
	from frappe.utils import getdate
	month, day = getattr(pack, "FINANCIAL_YEAR_START", (1, 1))
	d = getdate(d)
	start = date(d.year, month, day)
	return start if start <= d else date(d.year - 1, month, day)


def financial_year(pack, d) -> int:
	"""The year a date's financial year started in: 2026 for 10 Feb 2027
	where the year runs from 1 April (India), 2027 where it is the
	calendar year. Numbering that resets each year uses it."""
	return financial_year_start(pack, d).year


# ── children on the stay register (India DPDP s.9 and alike) ─────────────
# A minor is recorded with name and age only. ID details are kept only where
# registration law asks for them: by default for foreign nationals (Form C,
# TM30 and the like); a pack whose register covers every guest sets
# MINOR_IDS = "all".

def adult_age(pack) -> int:
	return int(getattr(pack, "ADULT_AGE", 18))


def minor_ids(pack) -> str:
	return getattr(pack, "MINOR_IDS", "foreign")


def is_minor(pack, age) -> bool:
	try:
		return age not in (None, "") and int(age) < adult_age(pack)
	except (TypeError, ValueError):
		return False


def minor_id_kept(pack, nationality: str | None) -> bool:
	"""May ID details be recorded for a minor of this nationality?"""
	if minor_ids(pack) == "all":
		return True
	home = (getattr(pack, "DEFAULT_NATIONALITY", "") or "").strip().casefold()
	nat = (nationality or "").strip().casefold()
	return bool(nat) and nat != home


def registration_card_required(pack) -> bool:
	"""Does registration law here expect a signed registration card (GRC)
	for every stay? The default for the guided check-in's GRC step."""
	return bool(getattr(pack, "REGISTRATION_CARD_REQUIRED", False))


def privacy_terms(pack) -> dict:
	"""Who a guest complains to, and the statutory guest report (if any)
	the hotel files - for the privacy notice where data is collected."""
	return {
		"privacy_authority": getattr(pack, "PRIVACY_AUTHORITY", None)
		                     or "your local data protection authority",
		"guest_report": getattr(pack, "GUEST_REPORT", None),
		"adult_age": adult_age(pack),
		# languages a guest may read the notice in besides the page's own
		"notice_languages": list(getattr(pack, "NOTICE_LANGUAGES", None) or []),
	}


def validate_id_type(property: str | None, id_type: str | None):
	"""An ID type must be one this property's country recognises. Values
	already on file are never re-checked - only new writes."""
	if not id_type:
		return
	allowed = id_types(pack_for(property))
	if id_type not in allowed:
		frappe.throw(
			frappe._("Unknown ID type {0}. Use one of: {1}.").format(
				id_type, ", ".join(allowed)))


# ── country integrations: payment gateways, tax returns, verification ────
# The core keeps the entry points (create a link, settle it, export a
# return, check an ID); the pack decides whether the country has the
# integration at all and which provider runs it.


def _not_offered(what: str):
	frappe.throw(frappe._("{0} is not available for this property's country.")
	             .format(what))


def payment_gateways(pack) -> list[str]:
	"""Online gateways the desk may configure in this country."""
	if not getattr(pack, "payment_provider", None):
		return []
	return list(getattr(pack, "PAYMENT_GATEWAYS", None) or [])


def _provider(pack, gateway: str | None):
	if not gateway or gateway not in payment_gateways(pack):
		_not_offered(frappe._("The {0} payment gateway").format(gateway or "online"))
	return pack.payment_provider(gateway)


def create_gateway_link(pack, settings, return_url: str | None = None,
                        **link) -> tuple[str, str]:
	"""A live payment link on the property's own gateway account.
	Returns (link id, url). `return_url`, where the gateway sends the guest
	after paying, reaches providers whose create_link takes it; the others
	keep their own page."""
	provider = _provider(pack, settings.gateway)
	if return_url and _takes(provider.create_link, "return_url"):
		link["return_url"] = return_url
	return provider.create_link(settings, **link)


def _takes(fn, name: str) -> bool:
	"""Whether `fn` accepts the keyword `name` (by name or through **kwargs)."""
	try:
		params = inspect.signature(fn).parameters
	except (TypeError, ValueError):
		return False
	return name in params or any(
		p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def test_gateway_link(pack, gateway: str | None, link_id: str) -> str:
	"""Where a test-mode link points - nothing is charged."""
	return _provider(pack, gateway).test_link(link_id)


def gateway_info(pack, gateway: str | None) -> dict:
	"""What Settings shows beside the keys: webhook URL, signup link."""
	return _provider(pack, gateway).info()


def _gateway_provider_any(gateway: str):
	"""The provider for a gateway name, from whichever pack offers it. A
	webhook names its gateway before it names a property."""
	mapping = frappe.get_hooks("kamra_localization") or {}
	for target in mapping.values():
		path = target[-1] if isinstance(target, (list, tuple)) else target
		try:
			pack = importlib.import_module(path)
		except ModuleNotFoundError:
			continue
		if gateway in payment_gateways(pack):
			return pack.payment_provider(gateway)
	return None


def parse_payment_webhook(gateway: str, payload: bytes, headers: dict,
                          settings_for) -> dict | None:
	"""A gateway's "paid" callback, verified against the property's own
	keys. Returns {folio, property, link_id, amount}, or None when the
	event is not a payment we act on. A bad signature raises."""
	provider = _gateway_provider_any(gateway)
	if not provider:
		return None
	folio = provider.webhook_folio(payload)
	if not folio or not frappe.db.exists("Folio", folio):
		return None
	property = frappe.db.get_value("Folio", folio, "property")
	if gateway not in payment_gateways(pack_for(property)):
		return None
	settings = settings_for(property)
	if settings.gateway != gateway:
		frappe.throw(frappe._("This property does not take payments through {0}.")
		             .format(gateway), frappe.PermissionError)
	paid = provider.verify_webhook(settings, payload, headers)
	if not paid:
		return None
	return {"folio": folio, "property": property, **paid}


def tax_return_rows(pack, property: str | None, from_date: str, to_date: str):
	fn = getattr(pack, "tax_return_rows", None)
	if not fn:
		_not_offered(frappe._("The tax return export"))
	return fn(property, from_date, to_date)


def verify_tax_id(pack, property: str, tax_id: str, business_name: str = ""):
	if not verify_released():
		frappe.throw(frappe._("Kamra Verify isn't available yet."))
	fn = getattr(pack, "verify_tax_id", None)
	if not fn:
		_not_offered(frappe._("Tax ID verification"))
	return fn(property, tax_id, business_name)


def verify_guest_id(pack, property: str, guest: str, id_type: str,
                    consent: int = 0):
	if not verify_released():
		frappe.throw(frappe._("Kamra Verify isn't available yet."))
	fn = getattr(pack, "verify_guest_id", None)
	if not fn:
		_not_offered(frappe._("Guest ID verification"))
	return fn(property, guest, id_type, consent)


def verify_released() -> bool:
	"""Kamra Verify isn't released yet: its hub service isn't live. Until it
	is, every Verify button, the Settings page and the endpoints stay off
	unless a site opts in with `kamra_verify_enabled: 1` in site config."""
	return bool(frappe.conf.get("kamra_verify_enabled"))


def integrations(pack) -> dict:
	"""Which country integrations this property has - so the desk shows a
	Verify button or a gateway picker only where they exist."""
	verify = verify_released()
	return {
		"payment_gateways": payment_gateways(pack),
		"tax_return": bool(getattr(pack, "tax_return_rows", None)),
		"verify_tax_id": verify and bool(getattr(pack, "verify_tax_id", None)),
		"verify_guest_id": verify and bool(getattr(pack, "verify_guest_id", None)),
	}


def supported_countries() -> list[dict]:
	"""Countries with a dedicated pack, and what picking one sets up - the
	setup wizard and Settings offer these. Anything else runs on the
	generic pack with the currency the operator chooses."""
	mapping = frappe.get_hooks("kamra_localization") or {}
	out = []
	for country, target in mapping.items():
		path = target[-1] if isinstance(target, (list, tuple)) else target
		try:
			pack = importlib.import_module(path)
		except ModuleNotFoundError:
			continue
		ctx = pack.invoice_context(frappe._dict(name=None))
		out.append({
			"country": country,
			"currency": getattr(pack, "DEFAULT_CURRENCY", None),
			"timezone": getattr(pack, "DEFAULT_TIMEZONE", None),
			"tax_label": ctx.get("tax_label"),
			"tax_id_label": ctx.get("tax_id_label"),
		})
	return sorted(out, key=lambda c: c["country"])


# ── statutory e-invoicing hooks ──────────────────────────────────────────
# A pack that must report invoices to its tax authority (Saudi ZATCA, and
# tomorrow others) implements on_invoice_issued / on_invoice_cancelled /
# on_pos_bill_paid. The core calls these after the bill is final; a
# failure is logged loudly but never stops the desk closing a bill - the
# record can be regenerated, a guest kept waiting at checkout cannot.


def _hook_record(name: str, args: tuple):
	"""The record an e-invoicing hook works on - whose buyer its error may
	quote: the bill (a folio), a POS order, or the printed source."""
	if name == "invoice_print_block" and len(args) >= 2:
		return (args[0], args[1])
	if not args:
		return None
	return ("POS Order" if name == "on_pos_bill_paid" else "Folio", args[0])


def _hook(pack, name, *args):
	fn = getattr(pack, name, None)
	if not fn:
		return None
	# all-or-nothing: a half-written record would fork the invoice chain
	sp = f"einv_{name}"
	frappe.db.savepoint(sp)
	try:
		return fn(*args)
	except Exception:
		frappe.db.rollback(save_point=sp)
		from kamra.privacy import guest_traceback
		frappe.log_error(title=f"e-invoicing: {name} failed",
		                 message=guest_traceback(_hook_record(name, args)))
		return None


def on_invoice_issued(pack, folio_name: str):
	return _hook(pack, "on_invoice_issued", folio_name)


def on_invoice_cancelled(pack, folio_name: str, invoice_number: str,
                         reason: str):
	return _hook(pack, "on_invoice_cancelled", folio_name, invoice_number,
	             reason)


def on_pos_bill_paid(pack, order_name: str, tax_rate: float):
	return _hook(pack, "on_pos_bill_paid", order_name, tax_rate)


def invoice_title(pack) -> str:
	"""What a settled bill is called. "Tax Invoice" only where the law asks
	for those words (a pack sets INVOICE_TITLE); everywhere else "Invoice"."""
	return getattr(pack, "INVOICE_TITLE", "Invoice")


def invoice_print_block(pack, source_doctype: str, source_name: str,
                        number: str | None = None):
	"""Whatever the authority requires ON the printed bill (ZATCA's QR and
	bilingual title) - None where nothing is required."""
	return _hook(pack, "invoice_print_block", source_doctype, source_name,
	             number)


# ── the folio's tax receipt, where someone else issues it ────────────────
#
# In some countries the receipt the tax office counts is not the hotel's
# bill: Uruguay's CFE is issued by an e-invoicing provider at check-in, and
# it can wait (no answer yet, contingency, the guest's residence missing).
# A pack like that implements
#
#   folio_fiscal_documents(folio) -> dict | None
#       {"title": "<what the receipt is called>",
#        "state": "pending" | "contingency" | "issued" | "attention",
#        "text": "<the state in words>", "notice": "<what the desk must do>",
#        "documents": [{"label": "<its type>", "state": <as above>,
#                       "number": "<series and number>" | None,
#                       "text": str, "url": "<its PDF>" | None, "date": str | None}],
#        "link": {"label": str, "url": str} | None}   the pack's own page for it
#
# and the folio shows it in a card of its own, open or settled, on screen.
# It is never the printed block of invoice_print_block (ZATCA's QR), which
# stays where it is. A state the desk does not know reads as "pending" -
# never as issued. A pack may also give the desk pages of its own, for the
# menu:
#
#   desk_pages(property) -> [{"label": str, "url": str, "roles": [str]}]
#
# Links go to this site ("/...") or over https; anything else is dropped.

FISCAL_STATES = ("pending", "contingency", "issued", "attention")


def _fiscal_state(value) -> str:
	return value if value in FISCAL_STATES else "pending"


def _safe_url(value) -> str | None:
	url = str(value or "").strip()
	if (url.startswith("/") and not url.startswith("//")) or url.startswith("https://"):
		return url
	return None


def _text(value) -> str:
	return str(value or "").strip()


def folio_fiscal_documents(pack, folio: str):
	raw = _hook(pack, "folio_fiscal_documents", folio)
	if not raw:
		return None
	link = raw.get("link") or {}
	link_url = _safe_url(link.get("url"))
	return {
		"title": _text(raw.get("title")),
		"state": _fiscal_state(raw.get("state")),
		"text": _text(raw.get("text")),
		"notice": _text(raw.get("notice")),
		"documents": [{
			"label": _text(doc.get("label")),
			"state": _fiscal_state(doc.get("state")),
			"number": _text(doc.get("number")) or None,
			"text": _text(doc.get("text")),
			"url": _safe_url(doc.get("url")),
			"date": _text(doc.get("date")) or None,
		} for doc in raw.get("documents") or []],
		"link": {"label": _text(link.get("label")), "url": link_url}
		if link_url and _text(link.get("label")) else None,
	}


def desk_pages(pack, property: str) -> list[dict]:
	fn = getattr(pack, "desk_pages", None)
	if not callable(fn):
		return []
	try:
		pages = fn(property) or []
	except Exception:
		frappe.log_error(title="localization: desk_pages failed",
		                 message=frappe.get_traceback())
		return []
	return [{"label": _text(p.get("label")), "url": _safe_url(p.get("url")),
	         "roles": [str(r) for r in p.get("roles") or []]}
	        for p in pages if _text(p.get("label")) and _safe_url(p.get("url"))]
