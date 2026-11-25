"""El recibo del hotel al cobrar una reserva antes del check-in (HR-349, D-HR-74: «con la reserva corresponde recibo
siempre»; la factura, al prestar el servicio).

Todo cobro pasa por una línea nueva del folio (`Folio.payments`): el aviso `payment.approved` de core
(kamra.payments.settle_payment_link, también con la tarjeta de la web), el enlace de pago y recepción
(kamra.api.add_folio_payment, record_advance). `on_folio_update` (on_update de Folio, hooks.py) mira solo las líneas
que trae ese guardado y, para cada una que la lleva (kamra_uy.receipts.model.kind_for), emite un UY Receipt:

- numerado por Property y sin huecos: el número sale de un contador propio de la serie (una fila de `tabSeries` por
  Property, `_next_number`), que solo bloquean los recibos; NO de la fila de la Property, que es el cerrojo del
  inventario de Kamra (HR-359). Si el cobro se deshace, el número se deshace con él (la unicidad de `property`+`number`
  lo guarda también);
- uno por línea de pago (`payment_row` único): el mismo aviso dos veces no asienta otra línea, así que no hace otro;
- con el emisor (razón social y RUT de la Property), el pagador, la fecha, el importe y la moneda del enlace, lo cobrado
  en pesos si Mercado Pago cobró en pesos, y la reserva y su estadía;
- la devolución de un pago con recibo deja su constancia, en la misma serie y con el importe negativo.

Nunca frena el cobro: si no se puede emitir, se deshace solo el recibo y su porqué (el tipo del error y la línea de pago,
sin datos del pagador) va al Error Log del folio; el barrido de cada 5 minutos (`sweep`, hooks.py) lo emite con el
número que le toque entonces y deja anotado en ese Error Log qué recibo salió (HR-359). El correo de confirmación lleva el enlace del recibo (kamra_uy.notifications.confirmation) y la reserva lo
enseña en sus conexiones (`reservation_dashboard`).
"""

from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from urllib.parse import urlencode

import frappe
from frappe.utils import add_to_date, get_datetime, get_url, getdate, nowdate

from kamra_uy.notifications.confirmation import LANGUAGE_FIELD
from kamra_uy.receipts import model

DOCTYPE = "UY Receipt"
LOG_TITLE = "kamra_uy: recibo del cobro de la reserva sin emitir"
_SAVEPOINT = "kamra_uy_receipt"
_QUIET = ("in_import", "in_install", "in_migrate", "in_patch")
# el contador de la serie de cada Property, en tabSeries (la tabla de las series de Frappe; varchar(100))
_SERIES = "kamra_uy.receipt:"
# lo que el Error Log del recibo sin emitir le dice al barrido, y lo que el barrido anota al resolverlo
_PENDING = re.compile(r"^payment_row=(\S+) kind=(Receipt|Refund)$", re.M)
RESOLVED = "Resuelto por el barrido de recibos:"
# un Error Log de antes de HR-359 no nombra la línea: el barrido mira las del folio guardadas poco antes del aviso
_LEGACY_WINDOW_MIN = 10
SWEEP_LIMIT = 50


def on_folio_update(doc, method=None) -> None:
	"""on_update de Folio: un recibo por cada cobro nuevo de una reserva antes del check-in, y la constancia de cada
	devolución de un pago con recibo."""
	if not doc.get("reservation") or any(frappe.flags.get(f) for f in _QUIET):
		return
	before = doc.get_doc_before_save()
	old = {p.name for p in (before.get("payments") or [])} if before else set()
	new = [p for p in doc.get("payments") or [] if p.name not in old]
	if not new:
		return
	status = frappe.db.get_value("Reservation", doc.reservation, "status")
	for row in new:
		kind = model.kind_for(row.payment_kind, row.amount, status)
		if not kind:
			continue
		refund_of = _receipt_refunded(doc, row) if kind == model.REFUND else None
		if kind == model.REFUND and not refund_of:
			continue
		_issue_safely(doc, row, kind, refund_of)


def _receipt_refunded(folio, row) -> str | None:
	"""El recibo del pago que devuelve `row`, o None si ese pago no tiene recibo."""
	target = model.refunded_payment(row.get("refund_of"), row.get("reference"))
	if not target:
		return None
	how, value = target
	if how == "reference":
		value = next((p.name for p in folio.payments if p.reference == value and p.payment_kind != "Refund"), None)
	return frappe.db.get_value(DOCTYPE, {"payment_row": value}, "name") if value else None


def _issue_safely(folio, row, kind: str, refund_of: str | None) -> None:
	frappe.db.savepoint(_SAVEPOINT)
	try:
		issue(folio, row, kind, refund_of)
	except Exception as e:
		frappe.db.rollback(save_point=_SAVEPOINT)
		# sin traza: ni el pagador ni las variables locales (HR-21); la línea de pago (su nombre, sin datos) y el tipo
		# de recibo, para el barrido
		frappe.log_error(title=LOG_TITLE, message=pending_note(type(e).__name__, row.name, kind),
		                 reference_doctype="Folio", reference_name=folio.name)
		return
	frappe.db.release_savepoint(_SAVEPOINT)


def pending_note(error: str, payment_row: str, kind: str) -> str:
	"""El texto del Error Log de un recibo sin emitir: el tipo del error y lo que el barrido necesita para emitirlo."""
	return (f"{error}\npayment_row={payment_row} kind={kind}\n"
	        "Lo emite el barrido de recibos de cada 5 minutos (kamra_uy.receipts.issue.sweep).")


def _series_key(prop: str) -> str:
	key = _SERIES + prop
	return key if len(key) <= 100 else _SERIES + hashlib.sha1(prop.encode()).hexdigest()


@contextmanager
def _latest_rows():
	"""Las lecturas con cerrojo de dentro leen la última versión confirmada, como en MariaDB < 11.6.

	Con innodb_snapshot_isolation (por defecto desde la 11.6.2), una lectura con cerrojo de una fila que otra
	transacción cambió después de la instantánea de esta da ER_CHECKREAD y MariaDB deshace la transacción ENTERA (el
	savepoint del recibo ya no existe y el cobro se pierde con él). El cobro casi siempre trae la instantánea de antes
	(ha leído la reserva y el folio), así que el contador de la serie se lee y se sube con la comprobación apagada solo
	durante esas tres sentencias, en esta sesión."""
	if frappe.db.db_type != "mariadb":
		yield
		return
	row = frappe.db.sql("show session variables like 'innodb_snapshot_isolation'")
	previous = str(row[0][1]).upper() if row else None
	if previous not in ("ON", "1"):
		yield
		return
	frappe.db.sql("set session innodb_snapshot_isolation = OFF")
	try:
		yield
	finally:
		frappe.db.sql("set session innodb_snapshot_isolation = ON")


def _next_number(prop: str) -> int:
	"""El siguiente número de la Property, de su contador en tabSeries (HR-359).

	Solo lo bloquean los recibos: la fila de la Property es el cerrojo de cada decisión de inventario de Kamra
	(lock_property_for_inventory) y el cobro no la espera. Dos cobros a la vez esperan su turno en el contador, hasta
	el final de la transacción del primero, y no repiten ni saltan número; si el cobro se deshace, el contador se
	deshace con él. La primera vez (o con el contador a cero) arranca del último recibo de la Property."""
	key = _series_key(prop)
	with _latest_rows():
		row = frappe.db.sql("select `current` from `tabSeries` where `name` = %s for update", key)
		if not row:
			# la primera vez (el barrido la siembra antes, ensure_series): un upsert toma el cerrojo exclusivo
			# directamente, así que dos a la vez esperan en vez de bloquearse como con insert ignore + for update
			frappe.db.sql("insert into `tabSeries` (`name`, `current`) values (%s, 0) "
			              "on duplicate key update `current` = `current`", key)
			row = frappe.db.sql("select `current` from `tabSeries` where `name` = %s for update", key)
		current = int(row[0][0] or 0)
		if not current:
			current = int(frappe.db.sql(f"select coalesce(max(number), 0) from `tab{DOCTYPE}` where property = %s "
			                            "for update", prop)[0][0] or 0)
		frappe.db.sql("update `tabSeries` set `current` = %s where `name` = %s", (current + 1, key))
	return current + 1


def issue(folio, row, kind: str, refund_of: str | None = None):
	"""Emite el recibo (o la constancia) de la línea `row` del folio `folio`. Idempotente por línea."""
	existing = frappe.db.get_value(DOCTYPE, {"payment_row": row.name}, "name")
	if existing:
		return frappe.get_doc(DOCTYPE, existing)
	res = frappe.db.get_value("Reservation", folio.reservation,
	                          ["name", "guest", "guest_name", "check_in_date", "check_out_date", LANGUAGE_FIELD],
	                          as_dict=True)
	prop = frappe.db.get_value("Property", folio.property,
	                           ["property_name", "legal_name", "gstin", "currency", "booking_language"], as_dict=True)
	payer = (frappe.db.get_value("Guest", res.guest, "full_name") if res.get("guest") else None) or res.guest_name
	home = (prop.currency or "").upper()
	extra = model.charged(row.get("currency"), row.get("fx_amount"), home)
	doc = frappe.get_doc({
		"doctype": DOCTYPE,
		"kind": kind,
		"number": _next_number(folio.property),
		"issued_on": getdate(row.get("posting_date") or nowdate()),
		"property": folio.property,
		"reservation": res.name,
		"folio": folio.name,
		"refund_of": refund_of,
		"issuer_name": (prop.legal_name or prop.property_name or folio.property).strip(),
		"issuer_rut": (prop.gstin or "").strip() or None,
		"payer_name": (payer or "").strip() or None,
		"check_in_date": res.check_in_date,
		"check_out_date": res.check_out_date,
		"amount": float(row.amount or 0),
		"currency": home,
		"charged_amount": (-extra["amount"] if float(row.amount or 0) < 0 else extra["amount"]) if extra else None,
		"charged_currency": extra["currency"] if extra else None,
		"exchange_rate": float(row.get("exchange_rate") or 0) or None,
		"payment_row": row.name,
		"payment_reference": row.get("reference"),
		"mode": row.get("mode"),
		"language": model.idioma(res.get(LANGUAGE_FIELD) or prop.booking_language),
		"access_key": frappe.generate_hash(length=32),
	})
	doc.receipt_no = model.number_label(doc.number)
	doc.insert(ignore_permissions=True)
	doc.db_set("receipt_url", url(doc), update_modified=False)
	return doc


def ensure_series(prop: str) -> None:
	"""Crea el contador de la serie de `prop` si no está, desde su último recibo, fuera del cobro: así el cobro solo
	bloquea una fila que ya existe y no espera los cerrojos de hueco de tabSeries (los de las series de nombres)."""
	key = _series_key(prop)
	if frappe.db.sql("select 1 from `tabSeries` where `name` = %s", key):
		return
	last = frappe.db.sql(f"select coalesce(max(number), 0) from `tab{DOCTYPE}` where property = %s", prop)[0][0]
	frappe.db.sql("insert ignore into `tabSeries` (`name`, `current`) values (%s, %s)", (key, int(last or 0)))


def sweep() -> dict:
	"""Cada 5 minutos (hooks.py): emite los recibos que no salieron al cobrar (Error Log «recibo del cobro de la reserva
	sin emitir» del folio) y anota en cada Error Log qué recibo salió, o por qué ya no toca.

	Cada uno en su transacción: la instantánea es de después de tomar el contador, así que el número es el siguiente
	de la serie aunque otro cobro haya emitido el suyo mientras tanto; `payment_row` único (y `issue`, idempotente por
	línea) impide el doble recibo si el aviso del pago volvió y ya lo emitió. Lo que falla se queda para la vuelta
	siguiente, sin otro Error Log; la devolución de un pago cuyo recibo sigue pendiente espera a que salga."""
	done = {"issued": 0, "resolved": 0, "failed": 0}
	for prop in frappe.get_all("Property", pluck="name"):
		ensure_series(prop)
	frappe.db.commit()
	logs = frappe.get_all("Error Log", filters={"method": LOG_TITLE, "reference_doctype": "Folio",
	                                            "error": ["not like", f"%{RESOLVED}%"]},
	                      fields=["name", "reference_name", "error", "creation"], order_by="creation asc",
	                      limit=SWEEP_LIMIT)
	frappe.db.commit()
	for log in logs:
		try:
			outcome = _sweep_log(log)
			frappe.db.commit()
		except Exception as e:
			frappe.db.rollback()
			done["failed"] += 1
			# sin traza ni datos: el Error Log del cobro sigue ahí y la vuelta siguiente lo reintenta
			frappe.logger("kamra_uy").warning(f"barrido de recibos: {log.name} sigue pendiente ({type(e).__name__})")
			continue
		if outcome == "issued":
			done["issued"] += 1
		elif outcome == "resolved":
			done["resolved"] += 1
	return done


def _sweep_log(log) -> str | None:
	"""Emite lo que debe el Error Log `log`: "issued" si salió algún recibo, "resolved" si ya no toca ninguno y None
	si queda pendiente (una devolución cuyo pago aún no tiene recibo)."""
	if not frappe.db.exists("Folio", log.reference_name):
		return _resolve(log, "el folio ya no existe")
	folio = frappe.get_doc("Folio", log.reference_name)
	if not folio.get("reservation"):
		return _resolve(log, "el folio no es de una reserva")
	rows = {p.name: p for p in folio.get("payments") or []}
	wanted = [(m.group(1), m.group(2)) for m in _PENDING.finditer(log.error or "")] or _legacy_rows(folio, log)
	issued, waiting, notes = [], False, []
	for row_name, kind in wanted:
		row = rows.get(row_name)
		if not row:
			notes.append(f"{row_name}: el cobro ya no está en el folio")
			continue
		existing = frappe.db.get_value(DOCTYPE, {"payment_row": row_name}, "name")
		if existing:
			notes.append(existing)
			continue
		refund_of = _receipt_refunded(folio, row) if kind == model.REFUND else None
		if kind == model.REFUND and not refund_of:
			waiting = True
			continue
		doc = issue(folio, row, kind, refund_of)
		issued.append(doc.name)
		notes.append(doc.name)
	# la devolución que llegó antes que el recibo de su pago se quedó sin constancia (on_folio_update la salta): sale ahora
	notes += _refunds_of(folio, set(issued))
	if waiting:
		return "issued" if issued else None
	_resolve(log, ", ".join(notes) or "no había cobro con recibo pendiente")
	return "issued" if issued else "resolved"


def _refunds_of(folio, receipts: set[str]) -> list[str]:
	"""Las constancias de las devoluciones del folio que devuelven un pago de `receipts` y no tienen la suya."""
	if not receipts:
		return []
	status = frappe.db.get_value("Reservation", folio.reservation, "status")
	out = []
	for row in folio.get("payments") or []:
		if model.kind_for(row.payment_kind, row.amount, status) != model.REFUND:
			continue
		refund_of = _receipt_refunded(folio, row)
		if refund_of in receipts and not frappe.db.exists(DOCTYPE, {"payment_row": row.name}):
			out.append(issue(folio, row, model.REFUND, refund_of).name)
	return out


def _legacy_rows(folio, log) -> list[tuple[str, str]]:
	"""Un Error Log de antes de HR-359 (solo el tipo del error): las líneas de pago del folio guardadas en los minutos
	de antes del aviso, sin recibo, que lo llevan con el estado de ahora de la reserva."""
	status = frappe.db.get_value("Reservation", folio.reservation, "status")
	until = get_datetime(log.creation)
	since = add_to_date(until, minutes=-_LEGACY_WINDOW_MIN)
	out = []
	for row in folio.get("payments") or []:
		if not row.get("creation") or not since <= get_datetime(row.creation) <= until:
			continue
		kind = model.kind_for(row.payment_kind, row.amount, status)
		if kind:
			out.append((row.name, kind))
	return out


def _resolve(log, note: str) -> str:
	frappe.db.set_value("Error Log", log.name, "error", f"{log.error or ''}\n{RESOLVED} {note}",
	                    update_modified=False)
	return "resolved"


def url(doc) -> str:
	"""La página del recibo para el huésped (www/recibo.py): el nombre y su clave, que no se adivina."""
	return get_url("/recibo?" + urlencode({"n": doc.name, "k": doc.access_key}))


def for_reservation(reservation: str) -> dict | None:
	"""El último recibo de un cobro de la reserva (el que la confirmó, en el correo de confirmación): {numero, url}."""
	rows = frappe.get_all(DOCTYPE, filters={"reservation": reservation, "kind": model.RECEIPT},
	                      fields=["name", "receipt_no", "access_key"], order_by="number desc", limit=1)
	if not rows:
		return None
	return {"numero": rows[0].receipt_no, "url": url(rows[0])}


def by_key(name: str | None, key: str | None) -> dict | None:
	"""El recibo `name` si `key` es su clave (comparación en tiempo constante); si no, None. Para /recibo."""
	import hmac

	if not name or not key or len(str(key)) > 64:
		return None
	row = frappe.db.get_value(DOCTYPE, str(name)[:140], "*", as_dict=True)
	if not row or not row.access_key or not hmac.compare_digest(str(row.access_key), str(key)):
		return None
	if row.refund_of:
		row["refund_of_no"] = frappe.db.get_value(DOCTYPE, row.refund_of, "receipt_no")
	return row


def reservation_dashboard(data=None):
	"""override_doctype_dashboards de Reservation: sus recibos, en las conexiones de la reserva."""
	data = frappe._dict(data or {})
	data.setdefault("transactions", [])
	data.setdefault("non_standard_fieldnames", {})
	if not data.get("fieldname"):
		data.fieldname = "reservation"
	elif data.fieldname != "reservation":
		data.non_standard_fieldnames[DOCTYPE] = "reservation"
	if not any(DOCTYPE in (g.get("items") or []) for g in data.transactions):
		data.transactions.append({"label": "Recibos", "items": [DOCTYPE]})
	return data
