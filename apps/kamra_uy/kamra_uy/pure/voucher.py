"""Por qué no vale un código de descuento (HR-377): el motivo, para que la web lo diga en el idioma del huésped. Sin frappe.

Kamra (kamra.pricing.validate_voucher) lanza una frase en inglés; la web de la reserva necesita saber QUÉ pasa para decirlo
con sus textos (`vale.error.*` de web/src/booking/textos.ts). Mismo orden de comprobación que Kamra: no existe → inactivo
→ aún no empieza → vencido → agotado → noches. La fecha de «hoy» es la del día de la reserva, como en Kamra (no las noches
de la estadía).
"""

from __future__ import annotations

from datetime import date


def _day(value) -> date | None:
	if not value:
		return None
	if isinstance(value, date):
		return value
	return date.fromisoformat(str(value)[:10])


def motivo(v: dict | None, hoy: date, noches: int) -> dict | None:
	"""None si el vale sirve para una estadía de `noches` reservada `hoy`; si no, {"reason": …} con "date"
	(AAAA-MM-DD) en «aun-no» y «vencido» y "min_nights" en «noches». `v` = los campos del Discount Voucher (disabled,
	valid_from, valid_to, max_uses, times_used, min_nights), o None si no existe."""
	if v is None:
		return {"reason": "no-existe"}
	if v.get("disabled"):
		return {"reason": "inactivo"}
	desde, hasta = _day(v.get("valid_from")), _day(v.get("valid_to"))
	if desde and desde > hoy:
		return {"reason": "aun-no", "date": desde.isoformat()}
	if hasta and hasta < hoy:
		return {"reason": "vencido", "date": hasta.isoformat()}
	if v.get("max_uses") and int(v.get("times_used") or 0) >= int(v["max_uses"]):
		return {"reason": "agotado"}
	minimo = int(v.get("min_nights") or 1)
	if noches < minimo:
		return {"reason": "noches", "min_nights": minimo}
	return None
