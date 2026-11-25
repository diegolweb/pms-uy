"""El conector fiscal de Kamra con core (HR-28): IVA de la estancia y CFE en el check-in, ajuste al cerrar la cuenta y
nota de crédito al anularla.

Cuándo (D-HR-32: la factura sale en el CHECK-IN, por la estancia entera):
- `adjust_iva` (validate de la reserva en casa): pregunta a core qué IVA lleva cada noche (`determineIva`, con el
  documento, su país y si su copia está en el archivo: D-HR-74) y lo deja en `night_rates`, que es lo que Kamra cobra.
  Si core no contesta, la reserva sigue con el IVA del paquete y el CFE lo vuelve a preguntar.
- `queue_task` (check-in, cierre de la cuenta, cuenta anulada, check-in deshecho): deja una tarea «Reconcile» en
  UY CFE dentro de la misma transacción y la encola tras el commit. Una tarea compara lo que debería estar facturado
  con lo que ya cubren los CFE y pide a core el CFE o la nota de crédito que falte (`model.reconcile`). Cada cargo
  cuenta para una sola reserva (`owner`): en un grupo, la noche que Kamra lleva al folio maestro es de su habitación, y
  al cerrar el maestro se concilia cada habitación con cargos en él (HR-362).
- `process_reservation` (cola, barrido de kamra_uy.fiscal.sync cada 5 minutos o recepción con «Reintentar»): procesa
  en orden las filas pendientes de una reserva. Core caído deja la fila «Pending» con su error, visible en el folio, y
  nada impide facturar ni cerrar la cuenta.

Credenciales: las del servicio de core de la propiedad, en su Payment Gateway Settings (HR-30: `key_id` = la propiedad
en core, `key_secret` = su clave con `fiscal:write`, `webhook_secret` = el secreto con que core firma los avisos).
"""

from __future__ import annotations

import json
from decimal import Decimal

import frappe
from frappe.utils import add_days, getdate

from kamra_uy.core.client import CoreClient, CoreError, core_base_url
from kamra_uy.fiscal import model, texts

DOCTYPE = "UY CFE"
COUNTRY = "Uruguay"
GATEWAY = "Mercado Pago"
CURRENCIES = ("UYU", "USD")
# el check-in espera a determineIva como mucho esto por intento (el cliente reintenta una vez)
INLINE_TIMEOUT = 4
JOB_TIMEOUT = 15
ROOM_CHARGES = ("Room", "Extra Guests", "Discount")
# lo que se cobra por noche, en la estancia (stay_lines) y en la cuenta: el descuento de la reserva se reparte entre
# esas noches (HR-235)
NIGHT_CHARGES = ("Room", "Extra Guests", "Meal Plan")


def commit() -> None:
	"""Lo que core ya aceptó queda guardado aunque falle lo siguiente (no dentro de un test: lo deshace él)."""
	if not frappe.flags.in_test:
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- each CFE core accepted is kept even if a later one fails


class FiscalError(Exception):
	"""Algo que no se arregla reintentando sin que una persona lo mire (moneda, tipo de cambio, 4xx de core)."""


class ResidenceMissingError(FiscalError):
	"""Sin el país del documento del huésped no se factura (HR-304, HR-346): la fila espera, «Pending», hasta que
	recepción lo indique (fiscal.events.on_guest_update la vuelve a procesar). También la espera de la cotización
	(fx.FxPendingError)."""


# ── propiedad y credenciales ─────────────────────────────────────────────────────────────────────────────────────
def is_uruguay(property: str | None) -> bool:
	from kamra.localization import property_country

	return bool(property) and property_country(property) == COUNTRY


def _session():
	"""La sesión HTTP del cliente (None: requests). Los tests la cambian por un core simulado."""
	return None


def client_for(property: str, timeout: float = JOB_TIMEOUT) -> tuple[str, CoreClient]:
	name = frappe.db.get_value("Payment Gateway Settings", {"property": property, "gateway": GATEWAY})
	settings = frappe.get_doc("Payment Gateway Settings", name) if name else None
	key = settings.get_password("key_secret", raise_exception=False) if settings else None
	if not settings or not settings.key_id or not key:
		raise CoreError(None, "not_configured", texts.NOT_CONFIGURED)
	return settings.key_id, CoreClient(core_base_url(), key, timeout=timeout, session=_session())


# ── lo que se sabe del huésped ───────────────────────────────────────────────────────────────────────────────────
def _iso(country: str | None) -> str | None:
	code = frappe.db.get_value("Country", country, "code") if country else None
	return (code or "").strip().upper() or None


def _nationality(value: str | None) -> str | None:
	"""El país (ISO) de la nacionalidad que guarda Kamra (model.nationality_code), o None."""
	from kamra_uy.localization import uruguay

	value = (value or "").strip()
	codes, names = set(), {}
	if len(value) == 2 and frappe.db.exists("Country", {"code": value.lower()}):
		codes = {value.upper()}
	elif len(value) > 3:
		code = frappe.db.get_value("Country", value, "code")
		names = {value.lower(): code.upper()} if code else {}
	return model.nationality_code(value, uruguay.DEFAULT_NATIONALITY, codes, names)


def _adult(occupant, adult_age: int) -> bool:
	age = occupant.age if occupant.age not in (None, "", 0) else occupant.guest_stated_age
	return not age or int(age) >= adult_age


def guests_annex(res, titular: dict) -> list[dict]:
	"""El anexo de huéspedes de la reserva (D-HR-74, pregunta 5): el titular y cada adulto que la comparte, solo los de
	documento extranjero. El IVA de la habitación va por el documento del titular (B, KICKOFF D-HR-74); la lista lleva
	a los demás. El país del documento de un acompañante (Stay Occupant no lo guarda) es el de su tipo o el de su
	nacionalidad: el pasaporte lo emite el país del que es."""
	from kamra.localization import adult_age, pack_for

	entries = [model.annex_entry(titular["name"], titular["nationality"], titular["doc"])]
	limit = adult_age(pack_for(res.property))
	for o in res.get("occupants") or []:
		if not _adult(o, limit):
			continue
		nationality = _nationality(o.nationality)
		entries.append(model.annex_entry(o.full_name, nationality, model.document(o.id_type, o.id_number, nationality)))
	out, seen = [], set()
	for e in entries:
		if e and (e["country"], e["doc_number"]) not in seen:
			seen.add((e["country"], e["doc_number"]))
			out.append(e)
	return out


def document_copy(res, guest_file: str | None) -> str | None:
	"""La copia del documento del huésped (la de la reserva, si no se descartó, o la de su ficha): la que va unida a
	la factura del no residente (D-HR-74, pregunta 1)."""
	if res.get("id_document") and not res.get("id_document_discarded"):
		return res.id_document
	return guest_file or None


def guest_inputs(res) -> dict:
	"""guest (para determineIva), copy_on_file, buyer y el anexo de huéspedes (para el CFE) y la copia del documento.
	Manda el país del documento (D-HR-74): el del tipo (la cédula es uruguaya) o el que indicó recepción
	(`Guest.uy_document_country`), nunca el de residencia, que va como dato. Sin él, ResidenceMissingError: nunca se
	supone (HR-304, HR-346)."""
	g = frappe._dict()
	if res.get("guest"):
		g = frappe.db.get_value("Guest", res.guest, ["full_name", "id_type", "id_number", "residence_country",
		                                             "uy_document_country", "nationality", "id_file"],
		                        as_dict=True) or frappe._dict()
	doc_country = model.document_country(g.id_type, _iso(g.uy_document_country))
	if not doc_country:
		raise ResidenceMissingError(texts.RESIDENCE_MISSING)
	copy_url = document_copy(res, g.id_file)
	facts = model.guest_facts(doc_country, g.id_type, bool(copy_url), residence=_iso(g.residence_country))
	doc = model.document(g.id_type, g.id_number, doc_country)
	rut, name = None, g.full_name or res.get("guest_name")
	if res.get("company"):
		company = frappe.db.get_value("Corporate Account", res.company, ["company_name", "gstin"], as_dict=True)
		if company and model.valid_rut("".join(ch for ch in (company.gstin or "") if ch.isdigit())):
			rut, name = company.gstin, company.company_name
	buyer = model.buyer(name, doc, rut=rut)
	annex = guests_annex(res, {"name": g.full_name or res.get("guest_name"), "doc": doc,
	                           "nationality": _nationality(g.nationality)}) if buyer["type"] == "rut" else []
	return {"guest": facts, "copy_on_file": bool(copy_url), "copy_url": copy_url, "document_country": doc_country,
	        "buyer": buyer, "annex": annex}


def _currency(property: str) -> str:
	currency = (frappe.get_cached_value("Property", property, "currency") or "").strip().upper()
	if currency not in CURRENCIES:
		raise FiscalError(texts.BAD_CURRENCY.format(currency=currency or "-"))
	return currency


def fx_quote(property: str, currency: str) -> tuple[str, str | None]:
	"""Pesos uruguayos por unidad de `currency` y el día de la cotización: la fiscal del BCU (kamra_uy.fiscal.fx,
	HR-348), el dólar interbancario comprador billete del día hábil anterior. Nunca el Exchange Rate de la caja. Sin
	cotización, fx.FxPendingError: la fila queda «Pending» y sale sola cuando llegue (cierra el HUECO H16)."""
	from kamra_uy.fiscal import fx

	return fx.quote(property, currency)


def fx_rate(property: str, currency: str) -> str:
	"""Pesos uruguayos por unidad de `currency` (el tipo de cambio que lleva el CFE)."""
	return fx_quote(property, currency)[0]


def invoice_currency(res) -> str:
	"""La moneda de las facturas de la reserva (HR-316): la que eligió recepción en su check-in (la de la tarea
	«Check-in» más reciente); sin ella, la del último CFE de la reserva (uno de antes de HR-316); y si no hay ninguno,
	la que se propone por la fecha del check-in (model.proposed_currency)."""
	for filters in ({"kind": "Reconcile", "purpose": "Check-in"}, {"kind": "Original"}):
		chosen = frappe.get_all(DOCTYPE, filters={"reservation": res.name, "currency": ["in", list(CURRENCIES)],
		                                          **filters},
		                        pluck="currency", order_by="creation desc, name desc", limit=1)
		if chosen:
			return chosen[0]
	return model.proposed_currency(res.check_in_date)


def _payments(folio: str | None) -> list[dict]:
	if not folio:
		return []
	return [{"mode": p.mode, "amount": p.amount} for p in frappe.get_doc("Folio", folio).payments
	        if p.payment_kind != "Refund" and p.mode]


# ── las líneas: la estancia (check-in) o la cuenta (cierre) ──────────────────────────────────────────────────────
def spread(amount, nights: dict[str, Decimal]) -> list[tuple[str, str]]:
	"""`amount` repartido entre las noches ({fecha: importe de la noche}), proporcional al importe de cada una y con el
	redondeo en la última, así que los trozos suman `amount` al centavo: [(fecha, trozo)] por orden de fecha. Una noche
	sin importe no lleva trozo; sin ninguna noche con importe no hay reparto ([]).

	Cada trozo va con la fecha de su noche, así que core le pone la tasa de esa noche (determineIva decide por línea,
	con su `date`): con la alta temporada (Decreto 267/001) una estancia puede tener noches exentas y noches al 10 %
	(HR-235)."""
	days = sorted((day, value) for day, value in nights.items() if value > 0)
	if not days:
		return []
	total = sum((value for _day, value in days), Decimal(0))
	whole = left = model.dec(model.money(amount))
	out = []
	for day, value in days[:-1]:
		piece = model.dec(model.money(whole * value / total))
		out.append((day, piece))
		left -= piece
	out.append((days[-1][0], left))
	return [(day, model.money(piece)) for day, piece in out if abs(piece) >= model.CENT]


def _night_amounts(lines: list[dict]) -> dict[str, Decimal]:
	out: dict[str, Decimal] = {}
	for ln in lines:
		out[ln["date"]] = out.get(ln["date"], Decimal(0)) + model.dec(ln["amount"])
	return out


def stay_lines(res) -> list[dict]:
	"""La estancia entera, noche a noche (habitación, huéspedes adicionales y comidas), y tras cada noche su parte del
	descuento de la reserva."""
	from kamra.folio import _nightly_room_rate, locked_night

	start = getdate(res.check_in_date)
	nights = max((getdate(res.check_out_date) - start).days, 1)
	room_type = (res.room_type or "").split("-")[-1]
	by_night: list[tuple[str, list[dict]]] = []
	for i in range(nights):
		day = add_days(start, i)
		key, shown = str(day), day.strftime("%d-%m-%Y")
		lines = [{"line_id": f"{key}:Room", "kind": "room", "date": key,
		          "amount": model.money(_nightly_room_rate(res, day)),
		          "description": texts.NIGHT.format(date=shown, room_type=room_type)}]
		extra = float(res.get("over_capacity_charge") or 0)
		if extra > 0:
			lines.append({"line_id": f"{key}:Extra Guests", "kind": "room", "date": key, "amount": model.money(extra),
			              "description": texts.EXTRA_GUESTS.format(date=shown)})
		locked = locked_night(res, day)
		meal = float(locked.get("meal_amount") or 0) if locked is not None and res.get("meal_plan") else 0
		if meal > 0:
			lines.append({"line_id": f"{key}:Meal Plan", "kind": "fnb", "date": key, "amount": model.money(meal),
			              "description": texts.MEAL_PLAN.format(date=shown)})
		by_night.append((key, lines))
	lines = [ln for _key, night in by_night for ln in night]
	discount = model.dec(res.get("discount_amount"))
	if discount <= 0:
		return lines
	pieces = dict(spread(-discount, _night_amounts(lines))) or {str(start): model.money(-discount)}
	out = []
	for key, night in by_night:
		out += night
		if key in pieces:
			out.append({"line_id": f"{key}:Discount", "kind": "room", "date": key, "amount": pieces[key],
			            "description": texts.DISCOUNT})
	return out


def _charge_line(c) -> dict:
	return {"line_id": c.name, "kind": model.kind_for(c.charge_type), "date": str(c.posting_date),
	        "amount": model.money(c.amount), "description": c.description or c.charge_type or ""}


def owner(charge, folio, rooms: set[str]) -> str:
	"""De qué reserva es un cargo: la que lleva el cargo (`Folio Charge.reservation`, con la que Kamra lo cargó o lo
	trasladó: kamra.folio._charge_posted) si es una habitación del grupo; si no, la del folio. En el folio maestro de un
	grupo (anclado a la reserva principal, kamra.folio.open_group_folio) la noche de cada habitación es de esa
	habitación, y lo que se cargó a mano en el maestro, de la principal, que es su pagador (HR-362)."""
	own = charge.get("reservation")
	return own if own and own in rooms else folio.reservation


def account_lines(folios: list, reservation: str | None = None, rooms: set[str] | None = None) -> list[dict]:
	"""Lo de las cuentas, cargo a cargo; con `reservation`, solo los cargos de esa reserva (`owner`, con las
	habitaciones de su grupo, `rooms`). El descuento de la reserva (que Kamra pone en el folio en un solo cargo, con la
	fecha del check-in) se reparte como en stay_lines: entre las noches que siguen cobradas en estas cuentas (sin las
	anuladas), proporcional al importe de cada una; cada trozo lleva `charge`, el cargo del folio de donde sale. Sin
	ninguna noche en las cuentas, el descuento va como está."""
	from kamra.folio import voided_rows

	def charges(f):
		if reservation is None:
			return list(f.charges)
		return [c for c in f.charges if owner(c, f, rooms or set()) == reservation]

	nights: dict[str, Decimal] = {}
	for f in folios:
		voided = voided_rows(f)
		for c in charges(f):
			if c.charge_type in NIGHT_CHARGES and model.dec(c.amount) > 0 and c.name not in voided:
				day = str(c.posting_date)
				nights[day] = nights.get(day, Decimal(0)) + model.dec(c.amount)
	lines = []
	for f in folios:
		for c in charges(f):
			if abs(float(c.amount or 0)) < 0.005:
				continue
			line = _charge_line(c)
			pieces = spread(c.amount, nights) if c.charge_type == "Discount" else []
			if not pieces:
				lines.append(line)
				continue
			lines += [{**line, "line_id": f"{c.name}:{day}", "date": day, "amount": piece, "charge": c.name}
			          for day, piece in pieces]
	return lines


def determine(client: CoreClient, core_property: str, res, lines: list[dict], inputs: dict,
              currency: str, payments: list[dict]) -> tuple[list[dict], str | None]:
	body = model.determine_request(core_property, lines, currency, inputs["guest"], payments,
	                               buyer_type=inputs["buyer"]["type"])
	out = client.post("/v1/fiscal/iva/determine", body, idempotency_key="")
	return model.with_rates(lines, out), out.get("rules_version")


# ── ajuste de IVA de la estancia (validate) ─────────────────────────────────────────────────────────────────────
def apply_room_rates(res, rates: dict[str, Decimal]) -> bool:
	"""Deja en `night_rates` el IVA que decidió el motor (lo que Kamra cobra cada noche) y corrige el total de
	impuestos de la reserva. True si cambió algo."""
	delta, changed = Decimal(0), False
	for row in res.get("night_rates") or []:
		rate = rates.get(str(getdate(row.stay_date)))
		old = Decimal(str(row.gst_rate or 0))
		if rate is None or rate == old:
			continue
		delta += Decimal(str(row.rate or 0)) * (rate - old) / 100
		row.gst_rate = float(rate)
		changed = True
	if changed:
		res.tax_amount = float(Decimal(str(res.tax_amount or 0)) + delta)
		res.amount_after_tax = float(Decimal(str(res.amount_before_tax or 0)) + Decimal(str(res.tax_amount)))
	return changed


def adjust_iva(res, timeout: float = INLINE_TIMEOUT) -> dict:
	"""Pregunta el IVA de cada noche y lo aplica a la reserva en memoria (la guarda quien llamó)."""
	core_property, client = client_for(res.property, timeout=timeout)
	inputs = guest_inputs(res)
	lines = stay_lines(res)
	desired, _ = determine(client, core_property, res, lines, inputs, _currency(res.property), [])
	apply_room_rates(res, model.room_rate_by_date(desired))
	return {"lines": desired, **inputs}


def _save_room_rates(res) -> None:
	"""Guarda lo que `apply_room_rates` dejó en la reserva en memoria (sin su validate ni otra versión)."""
	for row in res.get("night_rates") or []:
		frappe.db.set_value("Reservation Night Rate", row.name, "gst_rate", row.gst_rate, update_modified=False)
	frappe.db.set_value("Reservation", res.name, {"tax_amount": res.tax_amount,
	                                              "amount_after_tax": res.amount_after_tax}, update_modified=False)


def readjust(reservation: str) -> bool:
	"""Lo mismo para una reserva ya guardada (cambió el huésped): solo las noches que faltan por cobrar."""
	res = frappe.get_doc("Reservation", reservation)
	adjust_iva(res, timeout=JOB_TIMEOUT)
	_save_room_rates(res)
	return True


# ── tareas y CFE ─────────────────────────────────────────────────────────────────────────────────────────────────
def _guest_folio(reservation: str) -> str | None:
	return frappe.db.get_value("Folio", {"reservation": reservation, "folio_type": "Guest"}, order_by="creation desc")


def queue_task(res, purpose: str, folio: str | None = None, reason: str | None = None) -> str | None:
	if not is_uruguay(res.property):
		return None
	# la moneda de la factura (HR-316): la que eligió recepción en este check-in (kamra.api.check_in la deja en
	# res.flags, api.set_invoice_currency) o la propuesta; las demás tareas siguen con la del check-in
	if purpose == "Check-in":
		currency = res.flags.get("invoice_currency") or model.proposed_currency(res.check_in_date)
	else:
		currency = invoice_currency(res)
	doc = frappe.get_doc({"doctype": DOCTYPE, "kind": "Reconcile", "purpose": purpose, "status": "Pending",
	                      "property": res.property, "reservation": res.name, "currency": currency,
	                      "folio": folio or _guest_folio(res.name), "reason": (reason or "")[:500]})
	doc.insert(ignore_permissions=True)
	frappe.enqueue("kamra_uy.fiscal.connector.process_reservation", queue="short", enqueue_after_commit=True,
	               reservation=res.name)
	if purpose == "Close" and folio:
		kind, anchor = frappe.db.get_value("Folio", folio, ["folio_type", "reservation"]) or (None, None)
		if kind == "Group" and anchor == res.name:
			_close_rooms_of(folio, res, reason)
	return doc.name


def _close_rooms_of(folio: str, anchor, reason: str | None) -> None:
	"""Al cerrar el folio maestro de un grupo (que Kamra ancla a la reserva principal, y que solo le avisa a ella:
	events.on_folio_update), el ajuste de cierre de cada otra habitación con cargos en él (HR-362): lo suyo se concilia
	con lo que facturó su check-in. Las que no se alojaron, no: lo suyo lo factura fiscal.forfeit."""
	from kamra_uy.fiscal import forfeit

	doc = frappe.get_doc("Folio", folio)
	rooms = group_rooms(anchor)
	for name in dict.fromkeys(owner(c, doc, rooms) for c in doc.charges):
		if name == anchor.name:
			continue
		res = frappe.get_doc("Reservation", name)
		if res.status not in forfeit.STATUSES:
			queue_task(res, "Close", folio, reason)


def _rows(reservation: str, **filters) -> list:
	return frappe.get_all(DOCTYPE, filters={"reservation": reservation, **filters}, fields=["*"],
	                      order_by="creation asc, name asc")


def _lines(row) -> list | None:
	return json.loads(row.lines_json) if row.lines_json else None


def covered(reservation: str) -> list[dict]:
	live = [r for r in _rows(reservation) if r.kind != "Reconcile" and r.status in ("Sent", "Pending")
	        and r.core_status != "rejected"]
	originals = [{"row": r.name, "lines": _lines(r)} for r in live if r.kind == "Original"]
	credits = [{"references": r.references_cfe, "lines": _lines(r)} for r in live if r.kind == "Credit note"]
	return model.coverage(originals, credits)


def group_rooms(res) -> set[str]:
	"""Las reservas del grupo de la reserva (vacío si no es de un grupo)."""
	group = res.get("group_booking")
	if not group:
		return set()
	return set(frappe.get_all("Reservation", filters={"group_booking": group}, pluck="name"))


def _account_folios(res, rooms: set[str]) -> list:
	"""Las cuentas donde puede haber cargos de la reserva: las suyas cerradas, salvo «Account» (la de la cuenta
	corriente, que ya es de la empresa). En un grupo (HR-362), también las cerradas de las otras habitaciones (un cargo
	trasladado conserva su reserva) y los folios maestros del grupo, abiertos o cerrados: el maestro sigue abierto
	después de la salida de cada habitación y se cobra con el grupo (kamra.checkout.settlement_position), y las noches
	que lleva ya están facturadas en el check-in de cada una; mientras siga abierto no se devuelven."""
	own = {"status": "Closed", "folio_type": ["not in", ["Account", "Group"]]}
	names = frappe.get_all("Folio", filters={**own, "reservation": ["in", sorted(rooms or {res.name})]},
	                       pluck="name", order_by="creation asc")
	if rooms:
		names += frappe.get_all("Folio", filters={"group_booking": res.group_booking, "folio_type": "Group"},
		                        pluck="name", order_by="creation asc")
	else:
		# un folio «Group» sin grupo en la reserva (no lo abre Kamra así): cuenta como antes, cerrado
		names += frappe.get_all("Folio", filters={"reservation": res.name, "status": "Closed", "folio_type": "Group"},
		                        pluck="name", order_by="creation asc")
	return [frappe.get_doc("Folio", n) for n in dict.fromkeys(names)]


def _desired(task, res, client, core_property, inputs, currency) -> tuple[list[dict], str | None, list]:
	"""Lo que debería estar facturado: la estancia en el check-in; si no, los cargos de la reserva en todas sus
	cuentas cerradas (al anular una, vuelve a estar abierta y deja de contar) y en el folio maestro de su grupo
	(`_account_folios`). Cada cargo es de una sola reserva (`owner`): el ajuste de la reserva principal no factura las
	noches de las otras habitaciones, que ya salieron en su check-in (HR-362)."""
	if task.purpose == "Check-in":
		folios, lines, payments = [], stay_lines(res), _payments(task.folio)
	else:
		rooms = group_rooms(res)
		folios = _account_folios(res, rooms)
		lines = account_lines(folios, res.name, rooms)
		if rooms:
			# los pagos de las cuentas que llevan cargos de la reserva, no los de cada folio del grupo
			billed = {ln.get("charge") or ln["line_id"] for ln in lines}
			folios = [f for f in folios if any(c.name in billed for c in f.charges)]
		payments = [p for f in folios for p in _payments(f.name)]
	if not lines:
		return [], None, folios
	desired, version = determine(client, core_property, res, lines, inputs, currency, payments)
	return desired, version, folios


def _posted_at_other_rate(folios: list, desired: list[dict]) -> int:
	"""Los cargos del folio que Kamra cobró a otra tasa que la del CFE (el descuento repartido, si algún trozo va a
	otra)."""
	rates: dict[str, set[Decimal]] = {}
	for ln in desired:
		rates.setdefault(ln.get("charge") or ln["line_id"], set()).add(Decimal(ln["rate"]))
	return sum(1 for f in folios for c in f.charges
	           if c.name in rates and rates[c.name] != {Decimal(str(c.gst_rate or 0))})


def _run_task(task, res, client, core_property) -> None:
	home = _currency(res.property)
	invoiced_in = (task.currency or invoice_currency(res)).strip().upper()
	if invoiced_in not in CURRENCIES:
		raise FiscalError(texts.BAD_CURRENCY.format(currency=invoiced_in or "-"))
	inputs = guest_inputs(res)
	# el IVA se decide en la moneda de la reserva (lo que Kamra cobra); solo el CFE va en la de la factura
	desired, rules_version, folios = _desired(task, res, client, core_property, inputs, home)
	checked_in = task.purpose == "Check-in" and res.status == "Checked In"
	if checked_in and apply_room_rates(res, model.room_rate_by_date(desired)):
		# core no contestó en el check-in (events.on_reservation_validate) y las noches seguían con el IVA de la
		# cotización, 0 (HR-239): el folio cobra lo que factura este CFE, noche a noche
		_save_room_rates(res)
	plan = model.reconcile(desired, covered(res.name))
	if task.purpose != "Check-in" and res.status == "Checked In":
		# una factura intermedia con el huésped en casa: lo que falta se cobra al salir, sin notas de crédito a medias
		plan["credits"] = []
	created = []
	if plan["supplement"]:
		n = len(_rows(res.name, kind="Original")) + 1
		fx = fx_rate(res.property, invoiced_in)
		# en otra moneda que la de la reserva (HR-316): los importes, convertidos con la cotización del BCU; cada
		# línea guarda lo que era en la de la reserva, con lo que se concilia
		source_fx = fx if invoiced_in == home else fx_rate(res.property, home)
		rate = model.conversion(source_fx, fx)
		lines = model.convert_lines(plan["supplement"], rate)
		converted = {"source_currency": home, "source_fx_rate": source_fx} if invoiced_in != home else {}
		created.append({"kind": "Original", "reference_number": model.reference(res.name, n),
		                "currency": invoiced_in, **converted, "lines_json": json.dumps(lines),
		                "document_copy": inputs["copy_url"],
		                "request_json": json.dumps(model.cfe_request(
		                    core_property, task.folio or res.name, model.reference(res.name, n), inputs["buyer"],
		                    invoiced_in, fx, lines, model.convert_payments(_payments(task.folio), rate),
		                    rules_version, annex=inputs["annex"]))})
	reason = (task.reason or "").strip() or texts.REASON_CLOSE
	for credit in plan["credits"]:
		# la nota de crédito, en la moneda de su factura (core le pone la del CFE original)
		credited_in = frappe.db.get_value(DOCTYPE, credit["row"], "currency") or home
		created.append({"kind": "Credit note", "references_cfe": credit["row"], "reason": reason[:500],
		                "currency": credited_in, "lines_json": json.dumps(credit["lines"])})
	# las notas de crédito antes que el CFE nuevo: si la anulación se queda a medias, no hay nada facturado dos veces
	created.sort(key=lambda c: c["kind"] != "Credit note")
	for c in created:
		frappe.get_doc({"doctype": DOCTYPE, "status": "Pending", "property": res.property, "reservation": res.name,
		                "folio": task.folio, "task": task.name, "purpose": task.purpose, **c}
		               ).insert(ignore_permissions=True)
	other = _posted_at_other_rate(folios, desired)
	frappe.db.set_value(DOCTYPE, task.name, {
		"status": "Done" if created else "Not needed",
		"error": texts.POSTED_AT_OTHER_RATE.format(count=other) if other else ""})


def _send(row, client) -> dict:
	key = model.idempotency_key(frappe.local.site, row.name)
	if row.kind == "Original":
		return client.post("/v1/fiscal/cfe", json.loads(row.request_json), idempotency_key=key)
	original = frappe.db.get_value(DOCTYPE, row.references_cfe, "core_cfe_id")
	if not original:
		raise FiscalError(f"el CFE {row.references_cfe} no tiene id en el servicio")
	body = {"reason": row.reason or texts.REASON_CLOSE, "lines": model.credit_note_lines(_lines(row))}
	return client.post(f"/v1/fiscal/cfe/{original}/credit-note", body, idempotency_key=key)


def store_cfe(name: str, cfe: dict, status: str | None = None) -> None:
	fields = model.cfe_fields(cfe)
	values = {k: v for k, v in fields.items() if k not in ("total",)}
	values["total"] = fields["total"] or ""
	if status:
		values["status"] = status
	frappe.db.set_value(DOCTYPE, name, values)


def split(row, parts: list[dict]) -> int:
	"""Core no emite lo pedido en un solo comprobante (422 `split_required`, HR-345): cada parte sale en su propio CFE,
	con su número de factura y sus líneas, en filas nuevas «Pending»; la fila pedida queda «Not needed» con el motivo
	(ya no cuenta como cubierta). Devuelve cuántas partes."""
	pieces = model.split_request(json.loads(row.request_json), _lines(row) or [], parts)
	n = len(_rows(row.reservation, kind="Original"))
	for piece in pieces:
		n += 1
		number = model.reference(row.reservation, n)
		frappe.get_doc({"doctype": DOCTYPE, "kind": "Original", "status": "Pending", "property": row.property,
		                "reservation": row.reservation, "folio": row.folio, "task": row.task, "purpose": row.purpose,
		                "reference_number": number, "currency": row.currency, "source_currency": row.source_currency,
		                "source_fx_rate": row.source_fx_rate, "document_copy": row.get("document_copy"),
		                "lines_json": json.dumps(piece["lines"]),
		                "request_json": json.dumps({**piece["body"], "invoice_number": number})}
		               ).insert(ignore_permissions=True)
	titles = ", ".join(model.TYPE_TITLES.get(p["type"] or "", p["type"] or "") for p in parts)
	frappe.db.set_value(DOCTYPE, row.name, {"status": "Not needed",
	                                        "error": texts.SPLIT_INTO.format(count=len(parts), types=titles)})
	return len(parts)


def _fail(row, error: Exception, status: str) -> None:
	frappe.db.set_value(DOCTYPE, row.name, {"status": status, "error": str(error)[:1000],
	                                        "attempts": (row.attempts or 0) + 1})


def process_reservation(reservation: str) -> dict:
	"""Procesa en orden las filas pendientes de la reserva. Para en la primera que no sale: lo siguiente depende de
	ella. Devuelve {"sent", "pending", "attention"}."""
	frappe.db.sql("SELECT name FROM `tabReservation` WHERE name = %s FOR UPDATE", reservation)
	res = frappe.get_doc("Reservation", reservation)
	out = {"sent": 0, "pending": 0, "attention": 0}
	try:
		core_property, client = client_for(res.property)
	except CoreError as e:
		for row in _rows(reservation, status="Pending")[:1]:
			_fail(row, e, "Pending")
		out["pending"] = len(_rows(reservation, status="Pending"))
		return out
	while True:
		pending = _rows(reservation, status="Pending")
		if not pending:
			break
		row = pending[0]
		try:
			if row.kind == "Reconcile":
				_run_task(row, res, client, core_property)
			else:
				store_cfe(row.name, _send(row, client), status="Sent")
				out["sent"] += 1
		except CoreError as e:
			parts = model.split_parts(e.errors) if e.status == 422 and row.kind == "Original" else None
			if parts:
				# lo que core no emite junto (HR-345): sale en un CFE por parte, y se sigue con la primera
				split(row, parts)
				commit()
				continue
			retry = e.retryable or e.status is None
			_fail(row, e, "Pending" if retry else "Needs attention")
			out["pending" if retry else "attention"] += 1
			break
		except ResidenceMissingError as e:
			# espera a la residencia, sin pasar a Finanzas: en cuanto recepción la indica, sale sola
			_fail(row, e, "Pending")
			out["pending"] += 1
			break
		except (FiscalError, ValueError) as e:
			_fail(row, e, "Needs attention")
			out["attention"] += 1
			break
		commit()
	return out


def refresh(name: str) -> None:
	"""Pone al día un CFE con lo que dice core (por si se perdió un aviso)."""
	row = frappe.get_doc(DOCTYPE, name)
	_, client = client_for(row.property)
	cfe = client.get(f"/v1/fiscal/cfe/{row.core_cfe_id}")
	if model.is_newer(cfe.get("updated_at"), row.core_updated_at):
		store_cfe(name, cfe)
