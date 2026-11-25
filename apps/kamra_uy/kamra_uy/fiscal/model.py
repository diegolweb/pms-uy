"""La lógica del conector fiscal de Uruguay (HR-28), sin frappe: se prueba sola (tests/unit/fiscal).

- El documento: lo que el motor de IVA de core (`determineIva`) y el CFE necesitan del huésped, a partir de lo que
  guarda Kamra (`id_type`, `id_number`, el país del documento `Guest.uy_document_country` y la copia del documento).
  Manda el país del documento, no dónde vive (D-HR-74, preguntas 1 y 2; HR-346).
- Conciliación: Kamra pide a core lo que FALTA facturar. Las líneas deseadas (la estancia al check-in, o lo que dice
  la cuenta al cerrarla) se comparan, tasa a tasa, con lo que cubren los CFE ya pedidos menos sus notas de crédito.
  Lo que sobra sale en un CFE nuevo y lo que falta en una nota de crédito. El check-in (nada cubierto), la prolongación,
  la salida anticipada, el IVA corregido y la cuenta anulada (nada deseado) son el mismo cálculo.
- El bloque CFE de la factura impresa y la lectura del aviso `cfe.updated`.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from kamra_uy.fiscal import texts
from kamra_uy.pure import turismo

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
	"e-factura-exportacion": "e-Factura de Exportación",
	"nc-e-factura-exportacion": "Nota de crédito de e-Factura de Exportación",
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


# ── el documento (D-HR-74: manda el país del documento) ─────────────────────────────────────────────────────────────
# los tipos de Kamra cuyo país no hay que preguntar: la cédula («National ID» en el paquete de Uruguay) es uruguaya y el
# RG, brasileño; el pasaporte, el DNI y los demás llevan el país que indica recepción en el check-in
FIXED_DOCUMENT_COUNTRY = {"National ID": HOME_COUNTRY, "RG": "BR"}


def document_country(id_type: str | None, country: str | None) -> str | None:
	"""El país (ISO) que emitió el documento del huésped: el del tipo, si el tipo lo dice; si no, el que indicó
	recepción (`Guest.uy_document_country`). Nunca el de residencia. None si no se sabe: el check-in lo pide."""
	return FIXED_DOCUMENT_COUNTRY.get(id_type or "") or (country or "").strip().upper() or None


def document(id_type: str | None, id_number: str | None, country: str | None) -> dict | None:
	"""El documento del huésped en el formato del CFE, o None sin número o sin país. `country`: el país del documento
	que indicó recepción (`document_country`)."""
	number = (id_number or "").strip()[:40]
	country = document_country(id_type, country)
	if not number or not country:
		return None
	return {"doc_type": doc_type(id_type, country), "country": country, "doc_number": number}


def doc_type(id_type: str | None, country: str) -> str:
	"""El tipo de documento de la DGI: la cédula, el DNI (solo de AR, BR, CL o PY: tipo 6), el pasaporte u otro."""
	if id_type == "National ID":
		return "ci"
	if id_type == "RG" or (id_type == "DNI" and country in DNI_COUNTRIES):
		return "dni"
	return "passport" if id_type == "Passport" else "other"


class ResidenceMissing(ValueError):
	"""Sin el país del documento del huésped no se factura (HR-304, HR-346): nunca se supone."""


# lo que el huésped declaró al reservar (kamra_uy.pure.uy_tax.RESIDENT; el valor del campo uy_declared_residence). El
# valor guardado no cambió con D-HR-74: «Reside en Uruguay» quiere decir «documento uruguayo» (HR-350, HR-346)
DECLARED_RESIDENT = "Reside en Uruguay"


def invoice_residence(country: str | None, declared: str | None) -> str | None:
	"""El país de residencia (ISO) del huésped o, sin él, Uruguay si declaró al reservar que reside allí. Ya no decide
	el IVA (D-HR-74: manda `document_country`); se manda a determineIva como dato."""
	country = (country or "").strip().upper()
	if country:
		return country
	return HOME_COUNTRY if (declared or "").strip() == DECLARED_RESIDENT else None


def guest_facts(doc_country: str | None, id_type: str | None, copy_on_file: bool,
                residence: str | None = None) -> dict:
	"""`guest` de determineIva: el documento (su tipo, su país y si su copia está en el archivo) y, como dato, la
	residencia. Sin el país del documento, ResidenceMissing: nunca se supone (HR-304)."""
	country = document_country(id_type, doc_country)
	if not country:
		raise ResidenceMissing("sin país del documento")
	doc = {"country": country, "copy_on_file": bool(copy_on_file)}
	if id_type:
		doc = {"type": doc_type(id_type, country), **doc}
	guest = {"document": doc}
	residence = (residence or "").strip().upper()
	if residence:
		guest["residence_country"] = residence
	return guest


# lo que el huésped declaró al reservar frente al documento del check-in (D-HR-47: se cobra o se devuelve la diferencia)
def declared_mismatch(declared: str | None, doc_country: str | None) -> str | None:
	"""«collect» si declaró documento extranjero y el del check-in es uruguayo (paga el IVA que no pagó), «refund» si
	declaró documento uruguayo y es extranjero, None si coinciden o no hay declaración."""
	if not declared or not doc_country:
		return None
	said_home = declared.strip() == DECLARED_RESIDENT
	is_home = doc_country.strip().upper() == HOME_COUNTRY
	if said_home == is_home:
		return None
	return "collect" if is_home else "refund"


# ── el anexo de huéspedes (D-HR-74, pregunta 5; Decreto 220/998, art. 34, num. 14, lit. c) ─────────────────────────
# la categoría que da determineIva a la exportación (`tax_category`; iva-rules.uy.yaml): Kamra la guarda en sus líneas y
# no la manda en las del CFE, que core deduce de la regla
EXPORT_CATEGORY = "export_and_assimilated"
# lo que Kamra guarda de cada línea y no va en la petición del CFE (CfeLine no lo admite)
KAMRA_ONLY = ("source_amount", "tax_category")


def annex_entry(name: str | None, nationality: str | None, doc: dict | None) -> dict | None:
	"""Un huésped del anexo (`guests_annex`): nombre, nacionalidad y documento. Solo los de documento extranjero; sin
	nombre o sin documento, None. Sin nacionalidad reconocible, la del país del documento (que es quien lo emitió)."""
	name = (name or "").strip()[:150]
	if not name or not doc or doc["country"] == HOME_COUNTRY:
		return None
	return {"name": name, "nationality": (nationality or "").strip().upper() or doc["country"],
	        "country": doc["country"], "doc_type": doc["doc_type"], "doc_number": doc["doc_number"]}


# la nacionalidad que guarda Kamra es un texto: la del país (el gentilicio del paquete, «Uruguayan»), el nombre de un
# país o el código de 3 letras que lee el escaneo del pasaporte (kamra.id_scan, ICAO 9303). Los de 3 letras del mercado
# del hotel; uno que no está aquí toma el país del documento (annex_entry)
ISO3 = {"URY": "UY", "ARG": "AR", "BRA": "BR", "CHL": "CL", "PRY": "PY", "BOL": "BO", "PER": "PE", "COL": "CO",
        "VEN": "VE", "ECU": "EC", "MEX": "MX", "CRI": "CR", "PAN": "PA", "CUB": "CU", "DOM": "DO", "USA": "US",
        "CAN": "CA", "ESP": "ES", "PRT": "PT", "ITA": "IT", "FRA": "FR", "D": "DE", "DEU": "DE", "GBR": "GB",
        "IRL": "IE", "CHE": "CH", "AUT": "AT", "NLD": "NL", "BEL": "BE", "LUX": "LU", "DNK": "DK", "SWE": "SE",
        "NOR": "NO", "FIN": "FI", "POL": "PL", "CZE": "CZ", "HUN": "HU", "GRC": "GR", "RUS": "RU", "UKR": "UA",
        "ISR": "IL", "TUR": "TR", "CHN": "CN", "JPN": "JP", "KOR": "KR", "IND": "IN", "AUS": "AU", "NZL": "NZ",
        "ZAF": "ZA"}


def nationality_code(value: str | None, home_demonym: str, codes: set[str], names: dict[str, str]) -> str | None:
	"""El país (ISO) de una nacionalidad de Kamra, o None. `codes`: los ISO de 2 letras que existen; `names`: {nombre
	del país en minúsculas: ISO}."""
	value = (value or "").strip()
	if not value:
		return None
	if value == home_demonym:
		return HOME_COUNTRY
	if len(value) == 2 and value.upper() in codes:
		return value.upper()
	return ISO3.get(value.upper()) or names.get(value.lower())


def needs_annex(buyer_block: dict, lines: list[dict]) -> bool:
	"""El anexo va en la factura a una empresa (RUT) que lleva noches de exportación."""
	return buyer_block.get("type") == "rut" and any(ln.get("tax_category") == EXPORT_CATEGORY for ln in lines)


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
		if d.get("tax_category"):
			row["tax_category"] = d["tax_category"]
		if d.get("rebate"):
			row["rebate"] = d["rebate"]
		out.append(row)
	return out


def room_rate_by_date(lines: list[dict]) -> dict[str, Decimal]:
	return {ln["date"]: dec(ln["rate"]) for ln in lines if ln.get("kind") == "room"}


# ── conciliación ─────────────────────────────────────────────────────────────────────────────────────────────────
def _source(line: dict, invoiced: Decimal) -> Decimal:
	"""Lo que la línea es en la moneda de la reserva: `source_amount` si la factura va en otra moneda (HR-316); si no,
	lo mismo que en la factura."""
	return dec(line["source_amount"]) if line.get("source_amount") not in (None, "") else invoiced


def coverage(originals: list[dict], credits: list[dict]) -> list[dict]:
	"""Lo que sigue cubierto de cada línea de los CFE originales, descontadas sus notas de crédito (una sin líneas es
	total). Cada entrada: {row, line_id, rate, amount}, con `amount` en la moneda de la reserva (la que se concilia);
	si el CFE va en otra moneda (HR-316), también `invoice_amount`, lo que queda en la del CFE."""
	left: dict[tuple[str, str], list[Decimal]] = {}
	meta: dict[tuple[str, str], str] = {}
	for o in originals:
		for ln in o.get("lines") or []:
			key = (o["row"], ln["line_id"])
			invoiced = dec(ln.get("unit_amount")) * dec(ln.get("quantity") or 1)
			left[key] = [_source(ln, invoiced), invoiced]
			meta[key] = str(ln["rate"])
	for c in credits:
		if c.get("lines") is None:
			for key in left:
				if key[0] == c["references"]:
					left[key] = [Decimal(0), Decimal(0)]
			continue
		for ln in c["lines"]:
			key = (c["references"], ln["line_id"])
			if key in left:
				given = dec(ln.get("amount"))
				left[key][0] -= _source(ln, given)
				left[key][1] -= given
	out = []
	for k, (source, invoiced) in left.items():
		if source <= TOLERANCE:
			continue
		entry = {"row": k[0], "line_id": k[1], "rate": meta[k], "amount": money(source)}
		if money(invoiced) != money(source):
			entry["invoice_amount"] = money(invoiced)
		out.append(entry)
	return out


def _credit_line(covered_line: dict, take: Decimal) -> dict:
	"""Lo que se devuelve de una línea cubierta: `take` en la moneda de la reserva y, si el CFE va en otra, en la suya,
	a la misma proporción con que se convirtió esa línea (y lo que queda entero si se devuelve entera)."""
	source = dec(covered_line["amount"])
	if covered_line.get("invoice_amount") in (None, ""):
		return {"line_id": covered_line["line_id"], "amount": money(take)}
	invoiced = dec(covered_line["invoice_amount"])
	given = invoiced if source - take <= TOLERANCE else min(invoiced, dec(money(take * invoiced / source)))
	return {"line_id": covered_line["line_id"], "amount": money(given), "source_amount": money(take)}


def _cfe_line(line_id: str, description: str, amount, src: dict) -> dict:
	out = {"line_id": line_id, "description": (description or ADJUSTMENT_TEXT)[:250], "quantity": "1",
	       "unit_amount": money(amount), "rate": src["rate"], "reason_code": src["reason_code"],
	       "rule_id": src.get("rule_id")}
	if out["rule_id"] is None:
		del out["rule_id"]
	if src.get("rebate"):
		out["rebate"] = src["rebate"]
	if src.get("tax_category"):
		out["tax_category"] = src["tax_category"]
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
				credit_lines.setdefault(ln["row"], []).append(_credit_line(ln, take))
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
                lines: list[dict], payments: list[dict], rules_version: str | None,
                annex: list[dict] | None = None) -> dict:
	body = {
		"property": property_code,
		"folio": folio,
		"invoice_number": reference_number,
		"buyer": buyer_block,
		"currency": currency,
		"fx_rate": fx,
		"lines": [{k: v for k, v in ln.items() if k not in KAMRA_ONLY} for ln in lines],
		"payments": [{"mode": p["mode"], "amount": money(p["amount"])} for p in _positive_payments(payments)],
		"prices_include_iva": False,
	}
	if rules_version:
		body["rules_version"] = rules_version
	if annex and needs_annex(buyer_block, lines):
		body["guests_annex"] = annex[:200]
	return body


# ── lo que core no emite en un solo comprobante (HR-345: 422 `split_required` en /body/lines) ──────────────────────────
SPLIT_REASON = "split_required"


def split_parts(errors: list | None) -> list[dict] | None:
	"""Las partes de un 422 `split_required` de createCfe ([{type, line_ids}], dos o más), o None si el error es otro."""
	for e in errors or []:
		if isinstance(e, dict) and e.get("reason") == SPLIT_REASON and len(e.get("parts") or []) >= 2:
			return [{"type": p.get("type"), "line_ids": list(p.get("line_ids") or [])} for p in e["parts"]]
	return None


def _share(payments: list[dict], weight: Decimal, total: Decimal, last: bool, given: dict[int, Decimal]) -> list[dict]:
	out = []
	for i, p in enumerate(payments):
		whole = dec(p["amount"])
		amount = whole - given.get(i, Decimal(0)) if last else dec(money(whole * weight / total)) if total else Decimal(0)
		given[i] = given.get(i, Decimal(0)) + amount
		if amount > 0:
			out.append({**p, "amount": money(amount)})
	return out


def split_request(body: dict, lines: list[dict], parts: list[dict]) -> list[dict]:
	"""Cada parte de un CFE que core pide partir: {"lines": las líneas guardadas (con `source_amount`), "body": la
	petición sin `invoice_number`}. Cada línea va en una sola parte y todas en alguna (si no, ValueError: core contestó
	con otras líneas); los pagos se reparten en proporción al importe de cada parte, con el redondeo en la última."""
	by_id = {ln["line_id"]: ln for ln in lines}
	wanted = [lid for p in parts for lid in p["line_ids"]]
	if sorted(wanted) != sorted(by_id):
		raise ValueError("core pidió partir el CFE con otras líneas que las que se le mandaron")
	sent = {ln["line_id"]: ln for ln in body["lines"]}
	total = sum((dec(ln["unit_amount"]) * dec(ln.get("quantity") or 1) for ln in body["lines"]), Decimal(0))
	given: dict[int, Decimal] = {}
	out = []
	for n, part in enumerate(parts):
		part_lines = [sent[lid] for lid in part["line_ids"]]
		weight = sum((dec(ln["unit_amount"]) * dec(ln.get("quantity") or 1) for ln in part_lines), Decimal(0))
		piece = {**body, "lines": part_lines,
		         "payments": _share(body.get("payments") or [], weight, total, n == len(parts) - 1, given)}
		piece.pop("invoice_number", None)
		if "guests_annex" in body and not needs_annex(body["buyer"], [by_id[lid] for lid in part["line_ids"]]):
			piece.pop("guests_annex")
		out.append({"lines": [by_id[lid] for lid in part["line_ids"]], "body": piece})
	return out


# ── la moneda de la factura (HR-316, D-HR-57 y D-HR-60) ─────────────────────────────────────────────────────────
# Los precios van siempre en la moneda de la reserva (dólares: la tarifa, la web, la reserva y lo que cotiza
# recepción); solo la factura puede ir en la otra. Recepción la elige antes del check-in, que es cuando sale el CFE
# (D-HR-32). La conversión usa la cotización fiscal del BCU (kamra_uy.fiscal.fx, HR-348): el dólar interbancario
# comprador billete del día hábil anterior, el que toma Gsoft (D-HR-74, pregunta 7). Nunca el Exchange Rate de la caja.
INVOICE_CURRENCIES = ("USD", "UYU")
CURRENCY_TITLE = "Moneda de la factura"
CURRENCY_LABELS = {"USD": "Dólares (USD)", "UYU": "Pesos uruguayos (UYU)"}
PROPOSED_USD = "Se propone en dólares: el check-in cae en la alta temporada (del 16 de noviembre al domingo de Turismo)."
PROPOSED_UYU = "Se propone en pesos: el check-in cae fuera de la alta temporada."
FX_NOTE = "El CFE lleva la cotización del BCU del {day}: 1 {currency} = {rate} UYU."
CONVERTED_NOTE = "Los importes se convierten de {source} con la cotización del BCU del {day}: 1 USD = {rate} UYU."
FX_LINE = "Tipo de cambio: 1 {currency} = {rate} UYU"
CONVERTED_LINE = "Convertido de {source}: 1 USD = {rate} UYU"


def _day(value) -> date:
	return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def proposed_currency(check_in) -> str:
	"""Lo que se le propone a recepción: dólares si el check-in cae en la alta temporada (del 16 de noviembre al
	domingo de Turismo, los dos inclusive: kamra_uy.pure.turismo), pesos el resto del año."""
	return "USD" if turismo.en_alta_temporada(_day(check_in)) else "UYU"


def conversion(fx_source, fx_invoice) -> Decimal:
	"""Unidades de la moneda de la factura por unidad de la de la reserva, con los tipos de cambio a pesos de las dos
	(los mismos, a 6 decimales, que lleva el CFE): 40,123 de dólares a pesos; 1/40,123 de pesos a dólares."""
	return dec(fx_source) / dec(fx_invoice)


def convert_lines(lines: list[dict], rate: Decimal) -> list[dict]:
	"""Las líneas del CFE en la moneda de la factura, línea a línea al centavo. Cada una guarda en `source_amount` lo
	que era en la de la reserva (con eso se concilia). En la misma moneda, las líneas tal cual."""
	if rate == 1:
		return lines
	out = []
	for ln in lines:
		source = dec(ln["unit_amount"]) * dec(ln.get("quantity") or 1)
		out.append({**ln, "unit_amount": money(dec(ln["unit_amount"]) * rate), "source_amount": money(source)})
	return out


def convert_payments(payments: list[dict], rate: Decimal) -> list[dict]:
	if rate == 1:
		return payments
	return [{**p, "amount": money(dec(p.get("amount")) * rate)} for p in payments or []]


def credit_note_lines(lines: list[dict] | None) -> list[dict] | None:
	"""Lo que core recibe de cada línea de la nota de crédito: la línea y el importe en la moneda de su CFE."""
	if lines is None:
		return None
	return [{"line_id": ln["line_id"], "amount": ln["amount"]} for ln in lines]


def _rate_text(fx) -> str:
	return format(dec(fx).normalize(), "f").replace(".", ",")


def _day_text(value) -> str:
	return _day(value).strftime("%d-%m-%Y")


def currency_choice(home: str, check_in, fx, fx_day) -> dict:
	"""Lo que recepción ve en el check-in (kamra.localization, «the currency a stay is invoiced in»): las dos monedas,
	la propuesta y, en cada una, con qué cotización sale. `home`: la moneda de la reserva; `fx`: pesos por dólar (la
	cotización fiscal del BCU, fiscal.fx) y `fx_day`, su día; sin ella, que todavía no está y sale sola (nadie la teclea)."""
	proposed = proposed_currency(check_in)
	options = []
	for code in INVOICE_CURRENCIES:
		if not fx:
			note = texts.FX_PENDING
		elif code == home:
			note = FX_NOTE.format(currency=code, rate=_rate_text(fx), day=_day_text(fx_day)) if code != "UYU" else ""
		else:
			note = CONVERTED_NOTE.format(source=home, rate=_rate_text(fx), day=_day_text(fx_day))
		options.append({"code": code, "label": CURRENCY_LABELS[code], "note": note})
	return {"title": CURRENCY_TITLE, "text": PROPOSED_USD if proposed == "USD" else PROPOSED_UYU,
	        "options": options, "currency": proposed}


def fx_note(currency: str, source_currency: str | None, fx_rate, source_fx_rate) -> str:
	"""El tipo de cambio de un CFE, como lo escribe recepción en el portal: el del CFE si va en dólares; si va en pesos
	convertido de dólares, la cotización con que se convirtió (el CFE en pesos no lleva tipo de cambio)."""
	if currency and currency != "UYU" and fx_rate:
		return FX_LINE.format(currency=currency, rate=_rate_text(fx_rate))
	if source_currency and source_currency != currency and source_fx_rate:
		return CONVERTED_LINE.format(source=source_currency, rate=_rate_text(source_fx_rate))
	return ""


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
TYPE_CODES = {"e-ticket": (101, 201), "nc-e-ticket": (102, 202), "e-factura": (111, 211), "nc-e-factura": (112, 212),
              "e-factura-exportacion": (121, 221), "nc-e-factura-exportacion": (122, 222)}
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
