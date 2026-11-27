"""La seña perdida (HR-347): lo que el hotel se queda de una reserva cancelada fuera de plazo o de un no-show sale en un
e-Ticket (una e-Factura si paga una empresa con RUT) con el IVA del 22 % DENTRO de lo retenido, y su nota de crédito si
después se devuelve algo (D-HR-74, estudio contable, 24-11-2026, pregunta 4: «se emite ticket, con IVA»; no hay
servicio, así que no hay exportación). Al huésped que no vino no se le cobra nada más: lo cobrado es el total.

Qué es lo retenido: el mínimo entre los cargos de política que Kamra asentó en la cuenta («Cancellation fee (…)»,
«No-show charge (…)» y «Forfeited advance (no-show)», con sus impuestos y sin los anulados) y lo pagado neto de
devoluciones. Kamra asienta esos cargos con el IVA de la habitación (kamra.folio.post_policy_fee, `_room_gst`) y no deja
cambiarlo desde la localización: el comprobante se calcula aquí, con la línea `forfeit` que decide el motor de IVA
(determineIva; la regla es de core, HR-344), y Kamra no se toca.

Cuándo: al pasar la reserva a «Cancelled» (on_update) o al cambiar su cuenta mientras lo está o es «No Show» (el
no-show de la auditoría nocturna se escribe con db.set_value, sin ganchos de la reserva, y su cargo guarda el folio).
Cada vez se compara lo retenido con lo que ya cubren los CFE de la seña (como `model.reconcile`): falta → un CFE nuevo
por la diferencia; sobra → una nota de crédito por lo devuelto. Igual → nada, así que el mismo gancho dos veces deja
uno solo.

La tarea es una fila de UY CFE «Reconcile» con motivo «Forfeit» en estado «Queued», no «Pending»: así ni el barrido de
kamra_uy.fiscal.sync ni process_reservation la toman por una tarea de la estancia (connector._run_task facturaría la
cuenta). La procesan `process` (cola, tras el commit) y `sweep` (cada 5 minutos, hooks.py); lo que crea son filas
«Original» y «Credit note» normales, que envía el conector. Sin import de frappe arriba: lo puro se prueba sin él
(tests/unit/fiscal/test_forfeit.py).
"""

from __future__ import annotations

import json
from decimal import Decimal

from kamra_uy.fiscal import model
from kamra_uy.payments import payer

STATUSES = ("Cancelled", "No Show")
PURPOSE = "Forfeit"
QUEUED = "Queued"
# las descripciones con que Kamra asienta los cargos de política (kamra.folio.post_policy_fee, apply_no_show y
# NO_SHOW_FORFEIT de settle_no_show_credit; kamra.api._do_cancel)
FEE_PREFIXES = ("Cancellation fee (", "No-show charge (")
FORFEITED_ADVANCE = "Forfeited advance (no-show)"
PAID_KINDS = ("Payment", "Advance")
# lo que cuenta como ya cubierto: también lo que espera a una persona («Needs attention»), para no emitirlo dos veces
LIVE = ("Sent", "Pending", "Needs attention")

# textos (D-HR-48): la línea y el motivo salen en el CFE; los demás los ve recepción o Finanzas
LINE_CANCELLED = "Seña retenida por la cancelación de la reserva {reservation}"
LINE_NO_SHOW = "Seña retenida por no presentarse a la reserva {reservation}"
REASON_REFUND = "Devolución de la seña retenida"
DOC_MISSING = ("Falta el documento del comprador: por el importe, el comprobante de la seña tiene que identificarlo. "
               "Cárguelo en la ficha del huésped y sale solo.")
ALREADY_INVOICED = ("La reserva ya tiene facturada su estancia: la seña no lleva otro comprobante. Revíselo con "
                    "Finanzas.")
WAITING = "Espera a que salgan los demás comprobantes de la reserva."
# lo que contesta core cuando el e-Ticket pasa el tope sin documento (core: fiscal/cfe/compute.ts, checkBuyer)
CORE_DOC_PATH = "/body/buyer/doc_number"


# ── lo puro ──────────────────────────────────────────────────────────────────────────────────────────────────────
def is_forfeit_charge(charge: dict) -> bool:
	description = (charge.get("description") or "").strip()
	if charge.get("reversal_of") or charge.get("charge_type") != "Misc":
		return False
	return description == FORFEITED_ADVANCE or description.startswith(FEE_PREFIXES)


def _charge_total(charge: dict) -> Decimal:
	if charge.get("total") not in (None, ""):
		return model.dec(charge["total"])
	amount = model.dec(charge.get("amount"))
	return amount + amount * model.dec(charge.get("gst_rate")) / 100


def forfeit_charges(charges: list[dict]) -> list[dict]:
	"""Los cargos de política que siguen en pie (sin los anulados con su línea VOID)."""
	voided = {c["reversal_of"] for c in charges if c.get("reversal_of")}
	return [c for c in charges if is_forfeit_charge(c) and c.get("name") not in voided]


def paid(payments: list[dict]) -> Decimal:
	"""Lo pagado neto: pagos y anticipos menos las devoluciones (las filas «Refund» van en negativo). La fianza y su
	devolución no cuentan: no es dinero de la reserva."""
	deposits = {p.get("name") for p in payments if p.get("payment_kind") == "Security Deposit"}
	total = Decimal(0)
	for p in payments:
		kind = p.get("payment_kind") or "Payment"
		if kind in PAID_KINDS or (kind == "Refund" and p.get("refund_of") not in deposits):
			total += model.dec(p.get("amount"))
	return total


def retained(charges: list[dict], payments: list[dict]) -> Decimal:
	"""Lo que el hotel se queda: el mínimo entre los cargos de política con sus impuestos y lo pagado neto."""
	fees = sum((_charge_total(c) for c in forfeit_charges(charges)), Decimal(0))
	return max(Decimal(0), min(fees, paid(payments))).quantize(model.CENT)


def fee_date(charges: list[dict]) -> str | None:
	days = sorted(str(c.get("posting_date")) for c in forfeit_charges(charges) if c.get("posting_date"))
	return days[0] if days else None


def plan(keep: Decimal, covered: list[dict]) -> dict:
	"""{"supplement": lo que falta facturar (str) o None, "credits": [{row, lines: [{line_id, amount}]}]}.

	`covered`: lo que sigue cubierto de cada línea de los CFE de la seña (`model.coverage`, en la moneda de la reserva y
	con el IVA dentro). Lo devuelto sale de las últimas líneas hacia atrás. Menos de medio centavo no cuenta."""
	have = sum((model.dec(c["amount"]) for c in covered), Decimal(0))
	diff = model.dec(keep) - have
	out: dict = {"supplement": None, "credits": []}
	if diff > model.TOLERANCE:
		out["supplement"] = model.money(diff)
	elif diff < -model.TOLERANCE:
		owed, lines = -diff, {}
		for c in reversed(covered):
			if owed <= model.TOLERANCE:
				break
			take = min(owed, model.dec(c["amount"]))
			lines.setdefault(c["row"], []).append({"line_id": c["line_id"], "amount": model.money(take)})
			owed -= take
		out["credits"] = [{"row": row, "lines": ls} for row, ls in lines.items()]
	return out


def cfe_line(line_id: str, description: str, amount, determined: dict) -> dict:
	"""La línea del CFE con la tasa, el motivo y la regla que decidió el motor para la línea `forfeit`."""
	out = {"line_id": line_id, "description": description[:250], "quantity": "1", "unit_amount": model.money(amount),
	       "rate": str(determined["rate"]), "reason_code": determined["reason_code"]}
	if determined.get("rule_id"):
		out["rule_id"] = determined["rule_id"]
	if determined.get("rebate"):
		out["rebate"] = determined["rebate"]
	return out


def cfe_request(property_code: str, folio: str, reference_number: str, buyer: dict, currency: str, fx: str,
                lines: list[dict], payments: list[dict], rules_version: str | None) -> dict:
	"""El CFE de la seña: como el de la estancia, pero el importe lleva el IVA dentro (lo retenido es el total). Core
	elige el tipo por el comprador: e-Ticket al consumidor, e-Factura al RUT (compute.ts, chooseType)."""
	body = model.cfe_request(property_code, folio, reference_number, buyer, currency, fx, lines, payments,
	                         rules_version)
	body["prices_include_iva"] = True
	return body


def capped_payments(payments: list[dict], amount) -> list[dict]:
	"""Los pagos positivos en orden, hasta `amount`: lo retenido sale de ellos."""
	left, out = model.dec(amount), []
	for p in payments:
		if left <= model.TOLERANCE:
			break
		value = model.dec(p.get("amount"))
		if value <= 0 or (p.get("payment_kind") or "Payment") not in PAID_KINDS or not p.get("mode"):
			continue
		take = min(value, left)
		out.append({"mode": p["mode"], "amount": model.money(take)})
		left -= take
	return out


def needs_document(error: str | None) -> bool:
	return CORE_DOC_PATH in (error or "")


def has_identity(buyer: dict) -> bool:
	return bool(buyer.get("rut") or buyer.get("doc_number"))


# ── con frappe ───────────────────────────────────────────────────────────────────────────────────────────────────
def _frappe():
	import frappe

	return frappe


def _connector():
	from kamra_uy.fiscal import connector

	return connector


def _safe(title: str, fn, *args):
	"""Como fiscal.events._safe: un fallo queda en el registro de errores, nunca para a recepción."""
	frappe = _frappe()
	sp = "kamra_uy_forfeit"
	frappe.db.savepoint(sp)
	try:
		return fn(*args)
	except Exception:
		frappe.db.rollback(save_point=sp)
		frappe.log_error(title=f"kamra_uy fiscal: {title}")
		return None


def _folios(reservation: str) -> list:
	"""Las cuentas de la reserva que guardan su seña: las suyas, sin «Account» (la cuenta corriente de la empresa) ni
	«Group». El folio maestro de un grupo, aunque Kamra lo ancle a esta reserva, es dinero del grupo: paga las noches de
	las habitaciones, ya facturadas en su check-in, y nunca es la seña de su ancla (HR-375)."""
	frappe = _frappe()
	names = frappe.get_all("Folio", pluck="name", order_by="creation asc",
	                       filters={"reservation": reservation, "folio_type": ["not in", ["Account", "Group"]]})
	return [frappe.get_doc("Folio", n) for n in names]


def facts(reservation: str) -> tuple[list[dict], list[dict]]:
	"""(cargos, pagos) de las cuentas de la reserva, como dicts."""
	charges, payments = [], []
	for f in _folios(reservation):
		charges += [c.as_dict() for c in f.charges]
		payments += [p.as_dict() for p in f.payments]
	return charges, payments


def _rows(reservation: str, **filters) -> list:
	return _frappe().get_all(_connector().DOCTYPE, filters={"reservation": reservation, **filters}, fields=["*"],
	                         order_by="creation asc, name asc")


def covered(reservation: str) -> list[dict]:
	"""Lo que siguen cubriendo los CFE de la seña (los originales, menos sus notas de crédito)."""
	live = [r for r in _rows(reservation, purpose=PURPOSE) if r.kind != "Reconcile" and r.status in LIVE
	        and r.core_status != "rejected"]
	originals = [{"row": r.name, "lines": json.loads(r.lines_json or "[]")} for r in live if r.kind == "Original"]
	credits = [{"references": r.references_cfe, "lines": json.loads(r.lines_json) if r.lines_json else None}
	           for r in live if r.kind == "Credit note"]
	return model.coverage(originals, credits)


def _stay_invoiced(reservation: str) -> bool:
	"""Si la reserva tiene facturado algo que no es la seña (un CFE de antes de HR-347 por el cargo, o el del check-in
	sin su nota de crédito): entonces la seña ya está en un comprobante."""
	rows = [r for r in _connector()._rows(reservation) if r.purpose != PURPOSE and r.kind != "Reconcile"
	        and r.status in ("Sent", "Pending") and r.core_status != "rejected"]
	originals = [{"row": r.name, "lines": json.loads(r.lines_json or "[]")} for r in rows if r.kind == "Original"]
	credits = [{"references": r.references_cfe, "lines": json.loads(r.lines_json) if r.lines_json else None}
	           for r in rows if r.kind == "Credit note"]
	return bool(model.coverage(originals, credits))


def _buyer(res) -> dict:
	"""El receptor, sin pedir la residencia: la seña va a la tasa básica la tenga o no. Con RUT válido de la empresa de
	la reserva, e-Factura."""
	frappe, connector = _frappe(), _connector()
	g = frappe._dict()
	if res.get("guest"):
		g = frappe.db.get_value("Guest", res.guest, ["full_name", "id_type", "id_number", "uy_document_country"],
		                        as_dict=True) or frappe._dict()
	# el documento lleva el país que lo emitió, nunca el de residencia (HR-346, como connector._buyer); sin el del
	# huésped, el de quien pagó por Mercado Pago (HR-360, payments/payer.py)
	doc = model.document(g.id_type, g.id_number, connector._iso(g.uy_document_country)) or payer.document(res)
	rut, name = None, g.full_name or res.get("guest_name")
	if res.get("company"):
		company = frappe.db.get_value("Corporate Account", res.company, ["company_name", "gstin"], as_dict=True)
		if company and model.valid_rut("".join(ch for ch in (company.gstin or "") if ch.isdigit())):
			rut, name = company.gstin, company.company_name
	return model.buyer(name, doc, rut=rut)


def _due(res) -> bool:
	charges, payments = facts(res.name)
	have = sum((model.dec(c["amount"]) for c in covered(res.name)), Decimal(0))
	return abs(retained(charges, payments) - have) > model.TOLERANCE


def _waiting_document(reservation: str) -> list:
	return [r for r in _rows(reservation, purpose=PURPOSE, kind="Original", status="Needs attention")
	        if r.error == DOC_MISSING or needs_document(r.error)]


def queue(res) -> str | None:
	"""Deja la tarea de la seña («Queued») si hay algo que hacer y la encola tras el commit. Una sola por reserva."""
	frappe, connector = _frappe(), _connector()
	if res.status not in STATUSES or not connector.is_uruguay(res.property):
		return None
	task = frappe.db.get_value(connector.DOCTYPE, {"reservation": res.name, "kind": "Reconcile", "purpose": PURPOSE,
	                                               "status": QUEUED})
	if not task:
		# lo que espera el documento solo se rehace cuando ya está (o cuando cambia lo retenido: _due)
		if not _due(res) and not (_waiting_document(res.name) and has_identity(_buyer(res))):
			return None
		doc = frappe.get_doc({"doctype": connector.DOCTYPE, "kind": "Reconcile", "purpose": PURPOSE, "status": QUEUED,
		                      "property": res.property, "reservation": res.name,
		                      "currency": connector._currency(res.property),
		                      "folio": connector._guest_folio(res.name)})
		doc.insert(ignore_permissions=True)
		task = doc.name
	frappe.enqueue("kamra_uy.fiscal.forfeit.process", queue="short", enqueue_after_commit=True,
	               reservation=res.name)
	return task


# ── ganchos (hooks.py, doc_events) ───────────────────────────────────────────────────────────────────────────────
def on_reservation_update(doc, method=None):
	before = doc.get_doc_before_save()
	if doc.status in STATUSES and (before is None or before.status != doc.status):
		_safe("seña retenida", queue, doc)


def on_folio_update(doc, method=None):
	"""El cargo de no-show de la auditoría (la reserva ya es «No Show» sin sus ganchos), un pago, una devolución o un
	cargo anulado en la cuenta de una reserva que no se alojó."""
	frappe = _frappe()
	if not doc.reservation or doc.get("folio_type") in ("Account", "Group"):
		return
	if frappe.db.get_value("Reservation", doc.reservation, "status") not in STATUSES:
		return
	_safe("seña retenida", queue, frappe.get_doc("Reservation", doc.reservation))


def on_guest_update(doc, method=None):
	"""Recepción cargó el documento del huésped: el comprobante de la seña que lo esperaba (DOC_MISSING) se rehace."""
	frappe = _frappe()
	before = doc.get_doc_before_save()
	fields = ("id_type", "id_number", "uy_document_country", "residence_country", "full_name")
	if before is not None and all(before.get(f) == doc.get(f) for f in fields):
		return
	for name in frappe.get_all("Reservation", filters={"guest": doc.name, "status": ["in", list(STATUSES)]},
	                           pluck="name"):
		if _waiting_document(name):
			_safe("seña retenida: documento del comprador", queue, frappe.get_doc("Reservation", name))


# ── la tarea ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _set(name: str, values: dict) -> None:
	_frappe().db.set_value(_connector().DOCTYPE, name, values)


def _note_error(task, error: Exception | str) -> None:
	"""La tarea sigue «Queued» (la reintenta el barrido) con su último error; al registro de errores, solo si cambió."""
	text = str(error)[:1000]
	if text != (task.error or ""):
		_frappe().log_error(title="kamra_uy fiscal: seña retenida", message=f"{task.reservation}: {text}")
	_set(task.name, {"error": text, "attempts": (task.attempts or 0) + 1})


def _explain(reservation: str) -> None:
	"""El rechazo de core por falta de documento, en palabras de recepción (sale en /facturas y en la fila)."""
	for r in _rows(reservation, purpose=PURPOSE, kind="Original", status="Needs attention"):
		if needs_document(r.error):
			_set(r.name, {"error": DOC_MISSING})


def _line_text(res) -> str:
	text = LINE_NO_SHOW if res.status == "No Show" else LINE_CANCELLED
	return text.format(reservation=res.name)


def run(task, res) -> str:
	"""Concilia lo retenido con los CFE de la seña y deja los que faltan «Pending». Devuelve el estado de la tarea."""
	frappe, connector = _frappe(), _connector()
	if res.status not in STATUSES:
		return "Not needed"
	if _stay_invoiced(res.name):
		_set(task.name, {"error": ALREADY_INVOICED})
		frappe.log_error(title="kamra_uy fiscal: seña retenida", message=f"{res.name}: {ALREADY_INVOICED}")
		return "Not needed"
	buyer = _buyer(res)
	charges, payments = facts(res.name)
	home = connector._currency(res.property)
	keep = retained(charges, payments)
	steps = plan(keep, covered(res.name))
	waiting = _waiting_document(res.name)
	if waiting and (has_identity(buyer) or steps["supplement"] or steps["credits"]):
		# el comprobante que esperaba el documento nunca llegó a core: se rehace (fila nueva, clave nueva) con el
		# documento que ya está o con el importe nuevo, y el viejo deja de contar
		for r in waiting:
			_set(r.name, {"status": "Not needed"})
		steps = plan(keep, covered(res.name))
	created = []
	for credit in steps["credits"]:
		currency = frappe.db.get_value(connector.DOCTYPE, credit["row"], "currency") or home
		created.append({"kind": "Credit note", "references_cfe": credit["row"], "reason": REASON_REFUND,
		                "currency": currency, "lines_json": json.dumps(credit["lines"])})
	if steps["supplement"]:
		core_property, client = connector.client_for(res.property)
		fx = connector.fx_rate(res.property, home)
		pays = capped_payments(payments, steps["supplement"])
		n_line = len(_rows(res.name, purpose=PURPOSE, kind="Original")) + 1
		line = {"line_id": f"forfeit-{n_line}", "kind": "forfeit", "amount": steps["supplement"],
		        "date": fee_date(charges) or str(frappe.utils.today()), "description": _line_text(res)}
		determined, version = connector.determine(client, core_property, res, [line], {"guest": {}, "buyer": buyer},
		                                          home, pays)
		lines = [cfe_line(line["line_id"], line["description"], line["amount"], determined[0])]
		n = len(_rows(res.name, kind="Original")) + 1
		request = cfe_request(core_property, task.folio or res.name, model.reference(res.name, n), buyer, home, fx,
		                      lines, pays, version)
		created.append({"kind": "Original", "reference_number": model.reference(res.name, n), "currency": home,
		                "lines_json": json.dumps(lines), "request_json": json.dumps(request)})
	for c in created:
		frappe.get_doc({"doctype": connector.DOCTYPE, "status": "Pending", "property": res.property,
		                "reservation": res.name, "folio": task.folio, "task": task.name, "purpose": PURPOSE, **c}
		               ).insert(ignore_permissions=True)
	return "Done" if created else "Not needed"


def process(reservation: str) -> dict:
	"""La tarea de la seña de una reserva (cola o barrido): primero sale lo pendiente de la reserva (la nota de crédito
	de un check-in deshecho), luego se concilia la seña y se envía lo que deje."""
	frappe, connector = _frappe(), _connector()
	frappe.db.sql("SELECT name FROM `tabReservation` WHERE name = %s FOR UPDATE", reservation)
	tasks = _rows(reservation, kind="Reconcile", purpose=PURPOSE, status=QUEUED)
	if not tasks:
		return {"done": 0}
	from kamra_uy.core.client import CoreError

	connector.process_reservation(reservation)
	task = tasks[-1]
	for old in tasks[:-1]:
		_set(old.name, {"status": "Not needed"})
	if _rows(reservation, kind="Reconcile", status="Pending"):
		_note_error(task, WAITING)
		return {"done": 0}
	res = frappe.get_doc("Reservation", reservation)
	try:
		status = run(task, res)
	except (CoreError, connector.FiscalError, ValueError) as e:
		_note_error(task, e)
		return {"done": 0}
	_set(task.name, {"status": status})
	connector.commit()
	out = connector.process_reservation(reservation)
	_explain(reservation)
	return {"done": 1, **out}


def sweep() -> dict:
	"""Cada 5 minutos (hooks.py): las tareas de la seña que siguen «Queued» (core caído, sin cotización) y el rechazo
	por falta de documento de lo que reenvió el barrido del conector."""
	frappe, connector = _frappe(), _connector()
	done = {"reservations": 0, "failed": 0}
	names = frappe.get_all(connector.DOCTYPE, filters={"kind": "Reconcile", "purpose": PURPOSE, "status": QUEUED},
	                       pluck="reservation", order_by="creation asc", limit=200)
	for name in list(dict.fromkeys(n for n in names if n))[:50]:
		try:
			process(name)
			connector.commit()
			done["reservations"] += 1
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title="kamra_uy fiscal: barrido de la seña retenida")
			done["failed"] += 1
	for name in frappe.get_all(connector.DOCTYPE, filters={"purpose": PURPOSE, "kind": "Original",
	                                                       "status": "Needs attention",
	                                                       "error": ["like", f"%{CORE_DOC_PATH}%"]},
	                           pluck="reservation", limit=200):
		_explain(name)
	connector.commit()
	return done
