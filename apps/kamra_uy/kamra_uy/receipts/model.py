"""El recibo del hotel al cobrar una reserva (HR-349, D-HR-74), sin frappe: qué línea de pago lleva recibo, a qué
pago devuelve una devolución, qué dice el recibo y su cuerpo HTML.

- Recibo: el dinero que entra (`Payment` o `Advance`) en el folio de una reserva que todavía no llegó (Retenida,
  Pago pendiente o Confirmada: las mismas de `kamra.payments.settle_payment_link`). La web, el enlace de pago y
  recepción antes del check-in. Después del check-in el dinero lo cubre la factura (D-HR-32); la fianza
  (`Security Deposit`) no es un pago de la reserva.
- Constancia de devolución: una devolución (`Refund`) de un pago que tiene recibo, antes del check-in o con la reserva
  ya cancelada o no presentada, con el importe negativo. Después del check-in, lo que se devuelve va en la nota de
  crédito.

No es un CFE ni pasa por core (no es un CAE). Si Gsoft trae recibos y su API los emite, cambia `issue.py`.
"""

from __future__ import annotations

from datetime import date, datetime
from html import escape

from kamra_uy.notifications.formato import importe
from kamra_uy.receipts.texts import MONEDAS, TEXTOS

RECEIPT, REFUND = "Receipt", "Refund"
# las de kamra.payments.settle_payment_link: el dinero que llega antes que el huésped es un anticipo de la reserva
PRE_ARRIVAL = ("Pending Payment", "Held", "Confirmed")
# con la reserva en casa o cerrada, el dinero lo cubre la factura y lo devuelto, su nota de crédito
INVOICED = ("Checked In", "Checked Out")
MONEY_IN = ("Payment", "Advance")
IDIOMAS = tuple(TEXTOS)


def kind_for(payment_kind: str | None, amount, reservation_status: str | None) -> str | None:
	"""RECEIPT, REFUND o None para una línea de pago nueva del folio de una reserva en `reservation_status`. Un REFUND
	solo deja constancia si devuelve un pago con recibo (`refunded_payment`)."""
	value = float(amount or 0)
	if payment_kind in MONEY_IN and value > 0 and reservation_status in PRE_ARRIVAL:
		return RECEIPT
	if payment_kind == "Refund" and value < 0 and reservation_status and reservation_status not in INVOICED:
		return REFUND
	return None


def refunded_payment(refund_of: str | None, reference: str | None) -> tuple[str, str] | None:
	"""A qué pago devuelve una devolución: ("row", nombre de la línea) si recepción la hizo contra una línea
	(`refund_of`), ("reference", referencia del pago) si la asentó la pasarela («refund:<pago>:<id>» o
	«chargeback:<pago>», kamra.payments.gateway_refund_reference); None si no lo dice."""
	if refund_of:
		return "row", refund_of
	kind, _, rest = (reference or "").partition(":")
	if kind in ("refund", "chargeback") and rest:
		return "reference", rest.split(":", 1)[0]
	return None


def number_label(number: int) -> str:
	"""«R-12»: el número correlativo del recibo en su Property."""
	return f"R-{int(number)}"


def doc_name(property_code: str, number: int) -> str:
	"""El nombre del documento: el código corto de la Property (o su nombre) y el número, únicos en el sitio."""
	return f"{property_code}-{number_label(number)}"


def charged(row_currency: str | None, fx_amount, home_currency: str | None) -> dict | None:
	"""Lo cobrado en otra moneda que la del enlace (Mercado Pago cobra en pesos, D-HR-68; la línea de pago de Kamra lo
	guarda en `currency` y `fx_amount`): {currency, amount} o None si se cobró en la del enlace."""
	currency = (row_currency or "").strip().upper()
	if not currency or currency == (home_currency or "").strip().upper() or not float(fx_amount or 0):
		return None
	return {"currency": currency, "amount": round(abs(float(fx_amount)), 2)}


def _dia(valor) -> date:
	if isinstance(valor, datetime):
		return valor.date()
	if isinstance(valor, date):
		return valor
	return date.fromisoformat(str(valor)[:10])


def fecha(valor) -> str:
	"""«08/01/2027»: la fecha de un recibo, igual en los tres idiomas."""
	dia = _dia(valor)
	return f"{dia.day:02d}/{dia.month:02d}/{dia.year}"


def idioma(codigo: str | None) -> str:
	codigo = (codigo or "").strip().lower()[:2]
	return codigo if codigo in IDIOMAS else "es"


def view(r: dict, lang: str | None = None) -> dict:
	"""Lo que dice el recibo `r` (los campos de UY Receipt) en su idioma: título, filas (rótulo, valor) y aviso."""
	lang = idioma(lang or r.get("language"))
	t = TEXTOS[lang]
	refund = r.get("kind") == REFUND
	fechas = {"reserva": r["reservation"], "llegada": fecha(r["check_in_date"]), "salida": fecha(r["check_out_date"])}
	concepto = t["concepto_devolucion"].format(recibo=r.get("refund_of_no") or "", **fechas) if refund \
		else t["concepto_pago"].format(**fechas)
	emisor = r.get("issuer_name") or ""
	filas = [
		(t["numero"], r["receipt_no"]),
		(t["fecha"], fecha(r["issued_on"])),
		(t["emisor"], emisor),
	]
	if r.get("issuer_rut"):
		filas.append((t["rut"], r["issuer_rut"]))
	if r.get("payer_name"):
		filas.append((t["devuelto_a"] if refund else t["pagador"], r["payer_name"]))
	filas += [
		(t["concepto"], concepto),
		(t["importe"], importe(r["amount"], (r.get("currency") or "").upper(), lang)),
	]
	extra = charged(r.get("charged_currency"), r.get("charged_amount"), r.get("currency"))
	if extra:
		nombre = MONEDAS[lang].get(extra["currency"], extra["currency"])
		valor = -extra["amount"] if float(r["amount"] or 0) < 0 else extra["amount"]
		filas.append((t["cobrado"].format(moneda=nombre), importe(valor, extra["currency"], lang)))
	return {
		"lang": lang,
		"titulo": t["devolucion"] if refund else t["recibo"],
		"filas": filas,
		"aviso": t["aviso_devolucion"] if refund else t["aviso_recibo"],
		"emisor": emisor,
	}


def html(v: dict) -> str:
	"""El cuerpo del recibo (la página /recibo lo envuelve): todo lo que viene de la reserva va escapado."""
	filas = "".join(f'<tr><th scope="row">{escape(rotulo)}</th><td>{escape(str(valor))}</td></tr>'
	                for rotulo, valor in v["filas"])
	return (f"<h1>{escape(v['titulo'])}</h1>"
	        f'<table class="recibo">{filas}</table>'
	        f'<p class="aviso" data-aviso="factura">{escape(v["aviso"])}</p>')
