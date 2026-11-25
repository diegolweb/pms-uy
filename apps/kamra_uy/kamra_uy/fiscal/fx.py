"""La cotización de la factura (HR-348; D-HR-74, respuesta 7 del estudio contable).

El CFE en dólares lleva el dólar interbancario comprador billete del BCU (serie 2225, TCC) del último día hábil ANTERIOR
al día de la operación: el que toma Gsoft. Se pide al BCU (`kamra_uy.pure.bcu.fiscal_quote`) y nunca sale del Exchange
Rate de Kamra: esa es la cotización de la caja, con el margen de la casa, por fecha de negocio y tecleable a mano.

Sin cotización (el BCU no contesta o el día anterior todavía no está publicado), `fiscal_rate` da None y `quote` lanza
FxPendingError: la fila del CFE queda «Pending» y el barrido de kamra_uy.fiscal.sync la reintenta cada 5 minutos. Nadie
la teclea.

Lo consume `connector.fx_quote` (lo cablea HR-346): `from kamra_uy.fiscal import fx` dentro de la función y
`return fx.quote(property, currency)`. El import va dentro porque este módulo importa el conector.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

import frappe

from kamra_uy.fiscal import model
from kamra_uy.fiscal.connector import ResidenceMissingError
from kamra_uy.pure import bcu

HOME = "UYU"
# aprobado con D-HR-48 (22-11): lo ve recepción en la fila del CFE y en el check-in
FX_PENDING = ("Todavía no está la cotización del dólar del BCU del día anterior. No hace falta cargarla: el comprobante "
              "fiscal sale solo en cuanto llegue.")
# una cotización del BCU ya publicada no cambia; la que falta se vuelve a pedir como mucho cada 5 minutos (lo que
# tarda el barrido), para que un BCU caído no frene cada pantalla del check-in con su tope de tiempo. La que se toma
# saltando un día hábil sin cotización (feriado desde las 12, bcu.SETTLED_HOUR) también dura solo MISS_TTL: si el BCU
# publica tarde la que faltaba, la factura pasa a esa en el siguiente barrido y no se queda una semana con la de antes
# (HR-355)
HIT_TTL = 7 * 86400
MISS_TTL = 300
_KEY = "kamra_uy:fx_fiscal"


@dataclass(frozen=True)
class FiscalRate:
	rate: Decimal  # pesos uruguayos por 1 unidad de la moneda
	day: date | None  # el día de la cotización del BCU (None en pesos)


class FxPendingError(ResidenceMissingError):
	"""Sin la cotización del BCU no se factura en dólares: la fila espera «Pending» y el barrido la reintenta.
	Hereda de ResidenceMissingError porque es la espera que `connector.process_reservation` ya sabe tratar (sin pasar a
	Finanzas); el texto que guarda la fila es FX_PENDING, no el de la residencia."""


def _now(property: str) -> datetime:
	"""La hora de la propiedad (Montevideo), que es la que decide qué día es el de la operación."""
	from kamra.no_show import property_now

	return property_now(property)


def _code(currency: str) -> int:
	code = bcu.FISCAL_CODES.get(currency)
	if code is None:
		raise ValueError(f"la moneda {currency!r} no se factura con CFE (solo UYU o USD)")
	return code


def _lookup(code: int, on: date, now: datetime) -> tuple[bcu.BcuQuote | None, str]:
	key = f"{_KEY}:{code}:{bcu.FISCAL_SIDE}:{on.isoformat()}"
	# las dos claves caducan en Redis, pero set_value también las deja en frappe.local, que no caduca: se leen solo de Redis
	hit = frappe.cache.get_value(key, expires=True, use_local_cache=False)
	if hit:
		return bcu.BcuQuote(date.fromisoformat(hit[1]), code, Decimal(hit[0]), Decimal(hit[0])), ""
	miss = frappe.cache.get_value(f"{key}:miss", expires=True, use_local_cache=False)
	if miss:
		return None, miss
	from kamra_uy.localization import uruguay

	try:
		quote = bcu.fiscal_quote(uruguay.bcu_quotes([code], *bcu.fiscal_window(on)), code, on, now)
		reason = "" if quote else "el BCU todavía no publicó la cotización del día anterior"
	except Exception as e:
		quote, reason = None, f"el BCU no contestó ({type(e).__name__}: {e})"[:300]
	if quote is None:
		frappe.logger("kamra_uy.fx").warning(f"sin cotización fiscal del {on}: {reason}")
		frappe.cache.set_value(f"{key}:miss", reason, expires_in_sec=MISS_TTL)
		return None, reason
	rate = getattr(quote, bcu.FISCAL_SIDE.lower())
	skipped = any((quote.day + timedelta(days=k)).weekday() < 5 for k in range(1, (on - quote.day).days))
	frappe.cache.set_value(key, [str(rate), quote.day.isoformat()], expires_in_sec=MISS_TTL if skipped else HIT_TTL)
	return quote, ""


def fiscal_rate(property: str, currency: str, on: date | str | None = None) -> FiscalRate | None:
	"""Pesos uruguayos por 1 `currency` para facturar una operación del día `on` (por defecto, hoy en la propiedad), con
	el día de la cotización. En pesos, 1. None si todavía no hay cotización del BCU (nunca una fila manual ni con margen).
	"""
	currency = (currency or "").strip().upper()
	if currency == HOME:
		return FiscalRate(Decimal(1), None)
	code = _code(currency)
	now = _now(property)
	day = now.date() if on is None else on if isinstance(on, date) else date.fromisoformat(str(on))
	quote, _ = _lookup(code, day, now)
	return FiscalRate(getattr(quote, bcu.FISCAL_SIDE.lower()), quote.day) if quote else None


def quote(property: str, currency: str, on: date | str | None = None) -> tuple[str, str | None]:
	"""Lo que da `connector.fx_quote`: el tipo de cambio del CFE como texto y el día de su cotización, o FxPendingError
	si todavía no está."""
	found = fiscal_rate(property, currency, on)
	if found is None:
		raise FxPendingError(FX_PENDING)
	return model.fx_text(found.rate), found.day.isoformat() if found.day else None


def rate(property: str, currency: str, on: date | str | None = None) -> str:
	"""Pesos uruguayos por unidad de `currency`, el tipo de cambio que lleva el CFE."""
	return quote(property, currency, on)[0]


def diagnose(property: str, on: str | None = None) -> dict:
	"""Para el despliegue (`bench execute kamra_uy.fiscal.fx.diagnose --kwargs '{"property": "…"}'`): qué cotización del
	dólar toma hoy la factura y, si no hay, por qué. Pregunta al BCU aunque haya una guardada."""
	now = _now(property)
	day = date.fromisoformat(on) if on else now.date()
	code = _code("USD")
	frappe.cache.delete_value(f"{_KEY}:{code}:{bcu.FISCAL_SIDE}:{day.isoformat()}")
	frappe.cache.delete_value(f"{_KEY}:{code}:{bcu.FISCAL_SIDE}:{day.isoformat()}:miss")
	found, reason = _lookup(code, day, now)
	return {"operation_day": day.isoformat(), "series": code, "side": bcu.FISCAL_SIDE,
	        "rate": str(getattr(found, bcu.FISCAL_SIDE.lower())) if found else None,
	        "bcu_day": found.day.isoformat() if found else None, "reason": reason}
