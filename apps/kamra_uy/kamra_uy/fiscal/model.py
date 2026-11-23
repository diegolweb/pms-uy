"""La lógica del conector fiscal de Uruguay (HR-28), sin frappe: se prueba sola (tests/unit/fiscal).

- Residencia y documento: lo que el motor de IVA de core (`determineIva`) y el CFE necesitan del huésped, a partir de
  lo que guarda Kamra (`Guest.residence_country`, `id_type`, `id_number` y la copia del documento).
- Conciliación: Kamra pide a core lo que FALTA facturar. Las líneas deseadas (la estancia al check-in, o lo que dice
  la cuenta al cerrarla) se comparan, tasa a tasa, con lo que cubren los CFE ya pedidos menos sus notas de crédito.
  Lo que sobra sale en un CFE nuevo y lo que falta en una nota de crédito. El check-in (nada cubierto), la prolongación,
  la salida anticipada, el IVA corregido y la cuenta anulada (nada deseado) son el mismo cálculo.
- El bloque CFE de la factura impresa y la lectura del aviso `cfe.updated`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from kamra_uy.fiscal import texts

CENT = Decimal("0.01")
TOLERANCE = Decimal("0.005")
HOME_COUNTRY = "UY"
# el DNI solo vale como documento del receptor para estos países (formato de la DGI, tipo de documento 6)
DNI_COUNTRIES = {"AR", "BR", "CL", "PY"}
CFE_STATUSES = {"queued", "issued", "rejected", "contingency"}

ROOM_KINDS = {"Room", "Extra Guests", "Discount", "Early Check-in", "Late Check-out"}
FNB_KINDS = {"Meal Plan", "F&B", "Restaurant"}

# textos que salen en el CFE o en la factura (aprobados con D-HR-48, ver README.md)
CONSUMER_NAME = "Consumidor final"
ADJUSTMENT_TEXT = "Ajuste de la estancia"
VERIFY_TEXT = "Puede verificar el comprobante en www.dgi.gub.uy"
TYPE_TITLES = {
	"e-ticket": "e-Ticket",
	"e-factura": "e-Factura",
	"nc-e-ticket": "Nota de crédito de e-Ticket",
	"nc-e-factura": "Nota de crédito de e-Factura",
}
STATUS_TEXTS = {
	"pending": "Comprobante fiscal pendiente: el servicio de facturación no respondió y se reintenta solo.",
	"queued": "Comprobante fiscal en emisión.",
	"issued": "",
	"rejected": "El comprobante fiscal fue rechazado: revíselo en Finanzas.",
	"contingency": "Comprobante fiscal en contingencia: se emitirá en cuanto sea posible.",
}


# ── importes ─────────────────────────────────────────────────────────────────────────────────────────────────────
def dec(value) -> Decimal:
	return Decimal(str(value if value not in (None, "") else 0))


def money(value) -> str:
	return str(dec(value).quantize(CENT, rounding=ROUND_HALF_UP))


def fx_text(value) -> str:
	rate = dec(value).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
	if rate <= 0:
		raise ValueError("el tipo de cambio tiene que ser mayor que cero")
	return format(rate.normalize(), "f")


def _rate_key(rate) -> str:
	return format(dec(rate).normalize(), "f")


# ── residencia y documento ───────────────────────────────────────────────────────────────────────────────────────
def document(id_type: str | None, id_number: str | None, residence: str | None) -> dict | None:
	"""El documento del huésped en el formato del CFE, o None si no se puede decir de qué país es.

	Kamra no guarda el país del documento: la cédula («National ID» en el paquete de Uruguay) es uruguaya y el RG es
	brasileño; para el DNI, el pasaporte y los demás se toma el país de residencia."""
	number = (id_number or "").strip()[:40]
	if not number:
		return None
	residence = (residence or "").strip().upper() or None
	if id_type == "National ID":
		doc_type, country = "ci", HOME_COUNTRY
	elif id_type == "RG":
		doc_type, country = "dni", "BR"
	elif id_type == "DNI":
		doc_type, country = "dni", residence if residence in DNI_COUNTRIES else None
	elif id_type == "Passport":
		doc_type, country = "passport", residence
	else:
		doc_type, country = "other", residence
	if not country:
		return None
	return {"doc_type": doc_type, "country": country, "doc_number": number}


class ResidenceMissing(ValueError):
	"""Sin el país de residencia del huésped no se factura (HR-304): nunca se supone."""


# lo que el huésped declaró al reservar (kamra_uy.pure.uy_tax.RESIDENT; el valor del campo uy_declared_residence)
DECLARED_RESIDENT = "Reside en Uruguay"


def invoice_residence(country: str | None, declared: str | None) -> str | None:
	"""El país de residencia (ISO) con que se factura: el del huésped (`Guest.residence_country`, lo que recepción ve en
	el check-in con el documento); sin él, Uruguay si el huésped declaró al reservar que reside allí (lo dijo él, no se
	supone). Si no, None: sabemos, como mucho, que no reside, pero no dónde, y el check-in lo pide."""
	country = (country or "").strip().upper()
	if country:
		return country
	return HOME_COUNTRY if (declared or "").strip() == DECLARED_RESIDENT else None


def guest_facts(residence: str | None, id_type: str | None, id_number: str | None, copy_on_file: bool) -> dict:
	"""`guest` de determineIva. Sin país de residencia, ResidenceMissing: antes se tomaba Uruguay, y un extranjero en
	alta temporada pagaba el 10 % (HR-304)."""
	country = (residence or "").strip().upper()
	if not country:
		raise ResidenceMissing("sin país de residencia")
	guest = {"residence_country": country}
	doc = document(id_type, id_number, country)
	if doc:
		guest["document"] = {"type": doc["doc_type"], "country": doc["country"], "copy_on_file": bool(copy_on_file)}
	return guest


def kind_for(charge_type: str | None) -> str:
	if charge_type in ROOM_KINDS:
		return "room"
	if charge_type in FNB_KINDS:
		return "fnb"
	return "service"


def _positive_payments(payments: list[dict]) -> list[dict]:
	return [p for p in payments or [] if dec(p.get("amount")) > 0]


def determine_request(property_code: str, lines: list[dict], currency: str, guest: dict, payments: list[dict],
                      buyer_type: str = "consumer") -> dict:
	body = {
		"property": property_code,
		"lines": [{"line_id": ln["line_id"], "kind": ln["kind"], "amount": money(ln["amount"]), "currency": currency,
		           "date": ln["date"]} for ln in lines],
		"guest": guest,
		"billed_to": "guest_account",
		"buyer": {"type": buyer_type},
	}
	paid = _positive_payments(payments)
	if paid:
		body["payments"] = [{"mode": p["mode"], "amount": money(p["amount"]), "currency": currency} for p in paid]
	return body


def with_rates(lines: list[dict], determined: dict) -> list[dict]:
	"""Las líneas con la tasa, el motivo y la regla que decidió el motor."""
	by_id = {d["line_id"]: d for d in determined.get("lines") or []}
	out = []
	for ln in lines:
		d = by_id.get(ln["line_id"])
		if d is None:
			raise ValueError(f"el motor de IVA no contestó la línea {ln['line_id']}")
		row = {**ln, "rate": str(d["rate"]), "reason_code": d["reason_code"], "rule_id": d.get("rule_id"),
		       "pending_accountant": bool(d.get("pending_accountant"))}
		if d.get("rebate"):
			row["rebate"] = d["rebate"]
		out.append(row)
	return out


def room_rate_by_date(lines: list[dict]) -> dict[str, Decimal]:
	return {ln["date"]: dec(ln["rate"]) for ln in lines if ln.get("kind") == "room"}


# ── conciliación ─────────────────────────────────────────────────────────────────────────────────────────────────
def coverage(originals: list[dict], credits: list[dict]) -> list[dict]:
	"""Lo que sigue cubierto de cada línea de los CFE originales, descontadas sus notas de crédito (una sin líneas es
	total). Cada entrada: {row, line_id, rate, amount}."""
	left: dict[tuple[str, str], Decimal] = {}
	meta: dict[tuple[str, str], str] = {}
	for o in originals:
		for ln in o.get("lines") or []:
			key = (o["row"], ln["line_id"])
			left[key] = dec(ln.get("unit_amount")) * dec(ln.get("quantity") or 1)
			meta[key] = str(ln["rate"])
	for c in credits:
		if c.get("lines") is None:
			for key in left:
				if key[0] == c["references"]:
					left[key] = Decimal(0)
			continue
		for ln in c["lines"]:
			key = (c["references"], ln["line_id"])
			if key in left:
				left[key] -= dec(ln.get("amount"))
	return [{"row": k[0], "line_id": k[1], "rate": meta[k], "amount": money(v)}
	        for k, v in left.items() if v > TOLERANCE]


def _cfe_line(line_id: str, description: str, amount, src: dict) -> dict:
	out = {"line_id": line_id, "description": (description or ADJUSTMENT_TEXT)[:250], "quantity": "1",
	       "unit_amount": money(amount), "rate": src["rate"], "reason_code": src["reason_code"],
	       "rule_id": src.get("rule_id")}
	if out["rule_id"] is None:
		del out["rule_id"]
	if src.get("rebate"):
		out["rebate"] = src["rebate"]
	return out


def reconcile(desired: list[dict], covered: list[dict]) -> dict:
	"""{"supplement": líneas para un CFE nuevo, "credits": [{row, lines: [{line_id, amount}]}] para notas de crédito}.

	Tasa a tasa: sin nada cubierto, la estancia va línea a línea; con algo cubierto, la diferencia va en una línea de
	ajuste o se devuelve desde las últimas líneas cubiertas hacia atrás. Menos de medio centavo no cuenta."""
	rates: list[str] = []
	for ln in [*desired, *covered]:
		key = _rate_key(ln["rate"])
		if key not in rates:
			rates.append(key)
	supplement: list[dict] = []
	credit_lines: dict[str, list[dict]] = {}
	for key in rates:
		want = [ln for ln in desired if _rate_key(ln["rate"]) == key]
		have = [ln for ln in covered if _rate_key(ln["rate"]) == key]
		want_total = sum((dec(ln["amount"]) for ln in want), Decimal(0))
		have_total = sum((dec(ln["amount"]) for ln in have), Decimal(0))
		diff = want_total - have_total
		if abs(have_total) <= TOLERANCE:
			supplement += [_cfe_line(ln["line_id"], ln.get("description"), ln["amount"], ln) for ln in want]
		elif diff > TOLERANCE:
			supplement.append(_cfe_line(f"adj-{key}-{len(supplement) + 1}", ADJUSTMENT_TEXT, diff, want[0]))
		elif diff < -TOLERANCE:
			owed = -diff
			for ln in reversed(have):
				if owed <= TOLERANCE:
					break
				take = min(owed, dec(ln["amount"]))
				credit_lines.setdefault(ln["row"], []).append({"line_id": ln["line_id"], "amount": money(take)})
				owed -= take
	return {"supplement": supplement, "credits": [{"row": row, "lines": lines} for row, lines in credit_lines.items()]}


# ── petición de CFE ──────────────────────────────────────────────────────────────────────────────────────────────
RUT_WEIGHTS = (4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)


def valid_rut(digits: str) -> bool:
	if not re.fullmatch(r"\d{12}", digits or ""):
		return False
	check = 11 - sum(int(d) * w for d, w in zip(digits[:11], RUT_WEIGHTS, strict=True)) % 11
	check = 0 if check == 11 else check
	return check != 10 and check == int(digits[11])


def buyer(name: str | None, doc: dict | None, rut: str | None = None) -> dict:
	"""El receptor: e-Factura para una empresa con RUT válido; si no, consumidor final, con su documento si lo hay."""
	name = (name or "").strip()[:150] or CONSUMER_NAME
	digits = re.sub(r"\D", "", rut or "")
	if valid_rut(digits):
		return {"type": "rut", "name": name, "rut": digits}
	out = {"type": "consumer", "name": name}
	if doc:
		out.update(country=doc["country"], doc_type=doc["doc_type"], doc_number=doc["doc_number"])
	return out


def cfe_request(property_code: str, folio: str, reference_number: str, buyer_block: dict, currency: str, fx: str,
                lines: list[dict], payments: list[dict], rules_version: str | None) -> dict:
	body = {
		"property": property_code,
		"folio": folio,
		"invoice_number": reference_number,
		"buyer": buyer_block,
		"currency": currency,
		"fx_rate": fx,
		"lines": lines,
		"payments": [{"mode": p["mode"], "amount": money(p["amount"])} for p in _positive_payments(payments)],
		"prices_include_iva": False,
	}
	if rules_version:
		body["rules_version"] = rules_version
	return body


def reference(reservation: str, n: int) -> str:
	return f"{reservation}/{n}"


def idempotency_key(site: str, row: str) -> str:
	return f"kamra-uy-cfe:{site}:{row}"


# ── aviso cfe.updated ────────────────────────────────────────────────────────────────────────────────────────────
def parse_cfe_event(raw: bytes) -> dict:
	try:
		event = json.loads(raw)
	except (UnicodeDecodeError, ValueError):
		raise ValueError("el aviso no es JSON") from None
	if not isinstance(event, dict) or event.get("type") != "cfe.updated":
		raise ValueError("no es un aviso cfe.updated")
	for field in ("event_id", "property"):
		if not isinstance(event.get(field), str) or not event[field]:
			raise ValueError(f"al aviso le falta {field}")
	cfe = event.get("cfe")
	if not isinstance(cfe, dict) or not isinstance(cfe.get("cfe_id"), str) or cfe.get("status") not in CFE_STATUSES:
		raise ValueError("el aviso no trae un CFE válido")
	return event


def cfe_fields(cfe: dict) -> dict:
	"""Lo que Kamra guarda de un CFE de core (UY CFE)."""
	rng = cfe.get("cae_range") or {}
	err = cfe.get("error") or {}
	return {
		"core_cfe_id": cfe.get("cfe_id"),
		"core_status": cfe.get("status"),
		"cfe_type": cfe.get("type"),
		"serie": cfe.get("serie"),
		"number": cfe.get("number"),
		"cae": cfe.get("cae"),
		"cae_from": rng.get("from"),
		"cae_to": rng.get("to"),
		"cae_expires_on": cfe.get("cae_expires_on"),
		"security_code": cfe.get("security_code"),
		"qr_url": cfe.get("qr_url"),
		"pdf_url": cfe.get("pdf_url"),
		"dgi_status": cfe.get("dgi_status"),
		"issued_via": cfe.get("issued_via"),
		"issued_at": cfe.get("issued_at"),
		"contingency_since": cfe.get("contingency_since"),
		"overdue": bool(cfe.get("overdue")),
		"pending_accountant": bool(cfe.get("pending_accountant")),
		"total": cfe.get("total"),
		"currency": cfe.get("currency"),
		"error": f"{err.get('code')}: {err.get('message')}" if err else "",
		"core_updated_at": cfe.get("updated_at"),
	}


def _instant(value: str) -> datetime:
	return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_newer(new: str | None, old: str | None) -> bool:
	if not old:
		return True
	if not new:
		return False
	return _instant(new) > _instant(old)


# ── bloque impreso ───────────────────────────────────────────────────────────────────────────────────────────────
def print_block(fields: dict, issuer_rut: str | None) -> dict:
	"""Lo que la factura enseña del CFE: tipo, serie y número, código de seguridad, CAE, QR y la leyenda de la DGI.
	Sin CFE emitido todavía, el estado (pendiente, en cola o en contingencia) en lugar del número."""
	status = fields.get("core_status") or "pending"
	issued = status == "issued"
	cae = None
	if issued and fields.get("cae"):
		cae = {"number": fields["cae"], "from": fields.get("cae_from"), "to": fields.get("cae_to"),
		       "expires_on": fields.get("cae_expires_on")}
	return {
		"kind": "uy_cfe",
		"title": TYPE_TITLES.get(fields.get("cfe_type") or "", "CFE"),
		"status": status,
		"status_text": STATUS_TEXTS.get(status, ""),
		"contingency": status in ("pending", "contingency"),
		"serie": fields.get("serie") if issued else None,
		"number": fields.get("number") if issued else None,
		"security_code": fields.get("security_code") if issued else None,
		"qr_url": fields.get("qr_url") if issued else None,
		"pdf_url": fields.get("pdf_url") if issued else None,
		"cae": cae,
		"issuer_rut": issuer_rut,
		"verify_text": VERIFY_TEXT if issued else "",
	}


# ── el CFE en el folio de recepción (HR-307) ──────────────────────────────────────────────────────────────────────
# lo que el paquete de Uruguay da a kamra.localization.folio_fiscal_documents: la tarjeta del folio, abierto o
# cerrado (el CFE sale en el check-in), con los estados de Kamra: pending, contingency, issued y attention
DESK_TITLE = "Comprobante fiscal (CFE)"
PENDING_PAGE = "Facturas pendientes"
SERIE_NUMBER = "Serie {serie}, número {number}"
NEEDS_ATTENTION = "El comprobante fiscal quedó para revisar: avise a Finanzas."
# en contingencia, recepción lee lo que se hace (respuesta del fundador, 23-11): esperar al proveedor
DESK_CONTINGENCY = "Comprobante fiscal pendiente: se emite cuando vuelva el proveedor de facturas."


def desk_state(row: dict) -> str:
	"""El estado de un CFE (UY CFE, sin las tareas) para recepción. Lo que no se conoce es «pending»: nunca «issued»."""
	if row.get("status") == "Needs attention":
		return "attention"
	if row.get("status") == "Sent":
		return {"issued": "issued", "contingency": "contingency", "rejected": "attention"}.get(
			row.get("core_status") or "", "pending")
	return "pending"


def _desk_text(row: dict, state: str) -> str:
	if state == "attention":
		return STATUS_TEXTS["rejected"] if row.get("core_status") == "rejected" else NEEDS_ATTENTION
	if state == "pending":
		return STATUS_TEXTS["queued"] if row.get("status") == "Sent" else STATUS_TEXTS["pending"]
	if state == "contingency":
		return DESK_CONTINGENCY
	return STATUS_TEXTS.get(state, "")


def desk_block(rows: list[dict], tasks: list[dict], pending_url: str) -> dict | None:
	"""La tarjeta del CFE en el folio. `rows`: los UY CFE de la reserva (sin las tareas); `tasks`: sus tareas
	(Reconcile) que siguen «Pending» o «Needs attention». Sin nada de eso, None (no hay tarjeta). Mientras algo no esté
	emitido, el enlace a /facturas (`pending_url`), donde recepción lo registra o ve qué espera."""
	if not rows and not tasks:
		return None
	documents = []
	for row in rows:
		state = desk_state(row)
		issued = state == "issued"
		number = SERIE_NUMBER.format(serie=row["serie"], number=row["number"]) if (
			issued and row.get("serie") and row.get("number")) else None
		documents.append({
			"label": TYPE_TITLES.get(row.get("cfe_type") or "", texts.TYPE_PENDING),
			"state": state,
			"number": number,
			"text": "" if issued else _desk_text(row, state),
			"url": (row.get("pdf_url") or None) if issued else None,
			"date": str(row["issued_at"])[:10] if issued and row.get("issued_at") else None,
		})
	residence = any((t.get("error") or "") == texts.RESIDENCE_MISSING for t in tasks)
	states = {d["state"] for d in documents}
	if residence:
		state = "pending"
	elif "attention" in states or any(t.get("status") == "Needs attention" for t in tasks):
		state = "attention"
	elif "contingency" in states:
		state = "contingency"
	elif "pending" in states or tasks:
		state = "pending"
	else:
		state = "issued"
	return {
		"title": DESK_TITLE,
		"state": state,
		"text": STATUS_TEXTS["pending"] if not documents and not residence else "",
		"notice": texts.RESIDENCE_MISSING if residence else "",
		"documents": documents,
		"link": None if state == "issued" else {"label": PENDING_PAGE, "url": pending_url},
	}


# ── las facturas pendientes que recepción registra a mano (HR-304) ─────────────────────────────────────────────────
# el código de cada tipo (Tabla A; services/core/config/cfe.uy.yaml: cfe_types y contingency_types): el electrónico y
# el de contingencia, los dos que admite manual-issue
TYPE_CODES = {"e-ticket": (101, 201), "nc-e-ticket": (102, 202), "e-factura": (111, 211), "nc-e-factura": (112, 212)}
MANUAL_VIAS = ("portal", "paper")
# Uruguay no tiene horario de verano desde 2015: lo que recepción escribe es hora de Montevideo
MONTEVIDEO_OFFSET = "-03:00"


def is_pending(row: dict) -> bool:
	"""Si un CFE (UY CFE, sin las tareas) sigue sin emitir: aún no llegó a core o core lo tiene en cola o en
	contingencia (sin Gsoft conectado, todos). Lo emitido, por el proveedor o a mano, y lo rechazado no."""
	if row.get("kind") == "Reconcile":
		return False
	if row.get("status") in ("Pending", "Needs attention"):
		return True
	return row.get("status") == "Sent" and row.get("core_status") in ("queued", "contingency")


def type_codes(cfe_type: str | None) -> list[int]:
	"""Los códigos con que se puede registrar a mano un CFE de ese tipo; sin tipo (aún no lo eligió core), todos."""
	if cfe_type in TYPE_CODES:
		return list(TYPE_CODES[cfe_type])
	return [code for pair in TYPE_CODES.values() for code in pair]


def manual_issue(cfe_type: str | None, via: str, type_code, serie: str, number: str, issued_at: str,
                 note: str | None = None) -> dict:
	"""El cuerpo de manual-issue (recordManualCfe) con lo que recepción copia del portal o del papel, comprobado antes
	de mandarlo: ValueError con el motivo, en el español de recepción."""
	if via not in MANUAL_VIAS:
		raise ValueError("Indique si la factura se hizo en el portal o papel.")
	allowed = type_codes(cfe_type)
	try:
		code = int(str(type_code).strip())
	except ValueError:
		code = None
	if code not in allowed:
		raise ValueError(f"El tipo de comprobante tiene que ser {' o '.join(str(c) for c in allowed)}.")
	serie = (serie or "").strip().upper()
	if not re.fullmatch(r"[A-Z]{1,2}", serie):
		raise ValueError("La serie son una o dos letras, como la da el portal.")
	number = (number or "").strip()
	if not re.fullmatch(r"\d{1,7}", number):
		raise ValueError("El número son hasta 7 cifras, como lo da el portal.")
	issued = (issued_at or "").strip()
	if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", issued):
		issued += ":00" + MONTEVIDEO_OFFSET
	try:
		if "T" not in issued:
			raise ValueError(issued)
		_instant(issued)
	except ValueError:
		raise ValueError("Indique la fecha y la hora en que se emitió la factura.") from None
	body = {"via": via, "type_code": code, "serie": serie, "number": str(int(number)), "issued_at": issued}
	if (note or "").strip():
		body["note"] = note.strip()[:500]
	return body
