"""Payment Links for folio settlement.

Lightweight by design: one REST call out, one webhook in. Which gateways a
property may use is its country pack's call (India: Razorpay, Cashfree);
this module decides what a link is for and posts the money, and never
speaks a gateway's API itself.
"""

import frappe
from frappe.utils import nowdate

from kamra.authz import require_roles


def _settings(property: str):
	name = frappe.db.get_value(
		"Payment Gateway Settings", {"property": property, "enabled": 1})
	if not name:
		frappe.throw(
			"No payment gateway configured for this property. "
			"Add API keys under Payment Gateway Settings."
		)
	return frappe.get_doc("Payment Gateway Settings", name)


def create_payment_link(folio_name: str, amount: float | None = None,
                        purpose: str = "Stay bill") -> dict:
	"""A link for the folio's balance - or, before arrival, for an explicit
	amount (a booking deposit), when the folio has no charges yet."""
	from kamra import localization as loc

	folio = frappe.get_doc("Folio", folio_name)
	if folio.status == "Closed":
		frappe.throw("Folio is closed.")
	amount = float(amount) if amount else float(folio.balance or 0)
	if amount <= 0:
		frappe.throw("Nothing due on this folio.")
	pack = loc.pack_for(folio.property)
	if not loc.payment_gateways(pack):
		frappe.throw("Online payment links are not available for this property's country.")
	settings = _settings(folio.property)
	guest = frappe.get_doc("Guest", folio.guest)
	currency = frappe.get_cached_value("Property", folio.property,
	                                   "currency") or loc.default_currency(pack)

	if settings.test_mode:
		# local demo: fake link, settle via the webhook simulator
		link_id = f"plink_TEST{frappe.generate_hash(length=10)}"
		url = loc.test_gateway_link(pack, settings.gateway, link_id)
	else:
		link_id, url = loc.create_gateway_link(
			pack, settings,
			amount=amount,
			currency=currency.upper(),
			reference=folio.name,
			description=f"{purpose} {folio.name} · {folio.guest_name}",
			customer={"name": guest.full_name, "contact": guest.phone or "",
			          "email": guest.email or ""},
			notes={"folio": folio.name, "property": folio.property},
		)

	folio.db_set("payment_link_id", link_id, update_modified=False)
	folio.db_set("payment_link_url", url, update_modified=False)
	folio.db_set("payment_link_amount", amount, update_modified=False)

	from kamra.savings import log_action
	log_action("send_payment_link", "Folio", folio.name, folio.property,
	           minutes_saved=4,
	           rationale=f"{purpose} link {currency} {amount:,.0f} for {guest.full_name}",
	           channel="API")
	return {"url": url, "link_id": link_id, "amount": amount,
	        "currency": currency, "test_mode": bool(settings.test_mode)}


def settle_payment_link(folio_name: str, link_id: str, amount: float,
                        mode: str = "Payment Link",
                        reference: str | None = None) -> bool:
	"""Post money received through a link, once per link payment. Money that
	arrives before the guest does is an Advance on the booking: it counts
	toward the deposit and confirms a held booking.

	A gateway may report how the guest paid (mode) and which payment this
	is (reference): a link can be paid more than once, so the payment's own
	reference, when there is one, is what makes a retried event a no-op."""
	key = reference or link_id
	folio = frappe.get_doc("Folio", folio_name)
	if amount <= 0 or any(p.reference == key for p in folio.payments):
		return False
	res = frappe.get_doc("Reservation", folio.reservation) \
		if folio.reservation else None
	pre_arrival = bool(res and res.status in (
		"Confirmed", "Pending Payment", "Held"))
	folio.append("payments", {
		"posting_date": nowdate(),
		"payment_kind": "Advance" if pre_arrival else "Payment",
		"mode": mode or "Payment Link",
		"amount": amount,
		"reference": key,
	})
	from kamra.folio import _recalculate
	_recalculate(folio)
	folio.save(ignore_permissions=True)
	try:
		from kamra.ledger import record_payment_ledger
		record_payment_ledger(folio, folio.payments[-1].as_dict())
	except Exception:
		frappe.log_error(title="ledger payment-link write failed")
	if pre_arrival:
		frappe.db.set_value("Reservation", res.name, "advance_paid",
		                    float(res.advance_paid or 0) + amount)
		if res.status in ("Pending Payment", "Held"):
			from kamra.api import _confirm_status
			# set_value moved the booking on: saving the copy loaded above
			# would fail its timestamp check and undo advance_paid.
			res.reload()
			_confirm_status(res)
	from kamra.savings import log_action
	log_action("payment_received", "Folio", folio.name, folio.property,
	           minutes_saved=3,
	           rationale=f"{amount:,.0f} {'deposit ' if pre_arrival else ''}"
	                     f"auto-posted from payment link",
	           agent_name="Payments", channel="API")
	return True


def handle_webhook(gateway: str, payload: bytes, headers: dict) -> dict:
	"""Settle a gateway's "link paid" event. Idempotent: a retried event
	never posts the same payment twice. A bad signature raises. The
	provider may add mode and reference to what it parses (see
	settle_payment_link)."""
	from kamra import localization as loc

	paid = loc.parse_payment_webhook(gateway, payload, headers, _settings)
	if not paid:
		return {"ignored": True}
	posted = settle_payment_link(paid["folio"], paid["link_id"], paid["amount"],
	                             mode=paid.get("mode") or "Payment Link",
	                             reference=paid.get("reference"))
	return {"ok": True, "folio": paid["folio"], "posted": posted}


def gateway_refund_reference(kind: str, payment_reference: str,
                             refund_id: str | None = None) -> str:
	"""The reference of money a gateway gave back: it names the payment it
	returns - "refund:<payment>:<refund id>" or "chargeback:<payment>" - so
	refunded_for_payment can tell how much of each payment is left."""
	return ":".join(x for x in (kind, payment_reference, refund_id) if x)


def refunded_for_payment(folio, payment_reference: str) -> float:
	"""How much of one gateway payment has already come back (refunds and
	chargebacks posted by record_gateway_refund)."""
	total = 0.0
	for p in folio.payments:
		if p.payment_kind != "Refund" or not p.reference:
			continue
		rest = p.reference.partition(":")[2]
		if rest == payment_reference or rest.startswith(payment_reference + ":"):
			total += -float(p.amount or 0)
	return total


def record_gateway_refund(folio_name: str, reference: str, amount: float,
                          mode: str, reason: str) -> dict:
	"""Post money that already went back through the gateway - a refund or a
	chargeback - once per reference (see gateway_refund_reference).

	Unlike a desk refund there is no till (nothing left the drawer), no PIN
	and no guest-credit check: the money is gone, the folio has to say so.
	A pre-arrival deposit is released from Deposit and advance_paid drops,
	as with a desk refund. A closed folio is left alone."""
	if float(amount) <= 0:
		frappe.throw("Refund amount must be positive.")
	doc = frappe.get_doc("Folio", folio_name)
	if doc.status == "Closed":
		return {"posted": False, "reason": "folio_closed"}
	if any(p.reference == reference for p in doc.payments):
		return {"posted": False, "reason": "duplicate"}
	from kamra.api import _prearrival_advance_refund_ledger
	from kamra.business_date import get_business_date
	from kamra.localization import default_currency, money_text, pack_for
	ledger = _prearrival_advance_refund_ledger(doc)
	currency = (frappe.get_cached_value("Property", doc.property, "currency")
	            or default_currency(pack_for(doc.property)))
	doc.append("payments", {
		"posting_date": get_business_date(doc.property),
		"payment_kind": "Refund",
		"mode": mode or "Payment Link",
		"amount": -float(amount),
		"reference": reference,
		"currency": currency,
	})
	from kamra.folio import _recalculate
	_recalculate(doc)
	doc.save(ignore_permissions=True)
	try:
		from kamra.ledger import record_payment_ledger
		record_payment_ledger(doc, doc.payments[-1].as_dict(), ledger=ledger)
	except Exception:
		frappe.log_error(title="ledger gateway-refund write failed")
	if ledger == "Deposit" and doc.reservation:
		paid = float(frappe.db.get_value(
			"Reservation", doc.reservation, "advance_paid") or 0)
		frappe.db.set_value("Reservation", doc.reservation, "advance_paid",
		                    max(0.0, round(paid - float(amount), 2)))
	from kamra.savings import log_action
	log_action("folio_refund", "Folio", doc.name, doc.property,
	           rationale=f"{money_text(doc.property, amount)} returned at the "
	                     f"gateway ({reference}): {str(reason or '')[:100]}",
	           agent_name="Payments", channel="API")
	return {"posted": True, "balance": float(doc.balance or 0)}


@frappe.whitelist(methods=["POST"])
@require_roles("Finance", "Front Desk")
def refund_via_gateway(folio: str, reference: str, amount: float,
                       reason: str) -> dict:
	"""Ask the property's gateway to give back (part of) one payment.

	Posts nothing: the gateway reports the refund and record_gateway_refund
	posts it, so the folio has a single writer for gateway money. Returns the
	provider's {"status": "approved" | "pending" | "rejected", "message"?}."""
	from kamra import localization as loc

	amount = float(amount)
	if amount <= 0:
		frappe.throw("Refund amount must be positive.")
	if not (reason or "").strip():
		frappe.throw("A refund reason is required.")
	doc = frappe.get_doc("Folio", folio)
	if doc.status == "Closed":
		frappe.throw("This folio is closed.")
	paid = next((p for p in doc.payments
	             if p.reference == reference and p.payment_kind != "Refund"
	             and float(p.amount or 0) > 0), None)
	if not paid:
		frappe.throw(f"No gateway payment {reference} on this folio.")
	left = float(paid.amount) - refunded_for_payment(doc, reference)
	if amount > left + 0.005:
		frappe.throw(f"Only {loc.money_text(doc.property, left)} of this payment "
		             "is left to refund.")
	settings = _settings(doc.property)
	provider = loc._provider(loc.pack_for(doc.property), settings.gateway)
	refund = getattr(provider, "refund", None)
	if not callable(refund):
		loc._not_offered(frappe._("Refunds through {0}").format(settings.gateway))
	out = refund(settings, reference=reference, amount=amount,
	             reason=reason.strip())
	from kamra.savings import log_action
	log_action("gateway_refund_requested", "Folio", doc.name, doc.property,
	           rationale=f"{loc.money_text(doc.property, amount)} of {reference} "
	                     f"asked back at {settings.gateway}: "
	                     f"{(out or {}).get('status')}",
	           channel="API")
	return out


def _webhook(gateway: str) -> dict:
	headers = {k.lower(): v for k, v in (frappe.request.headers or {}).items()}
	out = handle_webhook(gateway, frappe.request.get_data() or b"{}", headers)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persists the completed operation before returning to an external/public caller; reviewed as intentional
	return out


@frappe.whitelist(allow_guest=True, methods=["POST"])
def razorpay_webhook():
	"""Razorpay calls this on payment_link.paid. Point the webhook at
	/api/method/kamra.payments.razorpay_webhook"""
	return _webhook("Razorpay")


@frappe.whitelist(allow_guest=True, methods=["POST"])
def cashfree_webhook():
	"""Cashfree posts here for every link (set per link as notify_url)."""
	return _webhook("Cashfree")


@frappe.whitelist()
@require_roles("Hotel Admin", "Finance")
def gateway_info(property: str, gateway: str = "") -> dict:
	"""What the Payments settings card shows beside the keys: where the
	gateway reports payments, and where to open an account."""
	from kamra import localization as loc

	pack = loc.pack_for(property)
	if not gateway:
		gateway = frappe.db.get_value("Payment Gateway Settings",
		                              {"property": property}, "gateway") \
			or (loc.payment_gateways(pack) or [""])[0]
	return loc.gateway_info(pack, gateway)
