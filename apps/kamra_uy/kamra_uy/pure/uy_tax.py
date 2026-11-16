"""Tasas de IVA del paquete uruguayo, sin frappe.

Los valores por defecto (habitación 10 %, comida, bebida y servicios 22 %) están pendientes del contador (HR-7): la
propiedad los cambia en sus campos `uy_iva_room_pct` y `uy_iva_standard_pct`, y un tipo de habitación con su
`tax_percent` distinto de 0 manda sobre la tasa de habitación de la propiedad.
"""

from decimal import Decimal, InvalidOperation

DEFAULT_ROOM_VAT = Decimal("10")
DEFAULT_STANDARD_VAT = Decimal("22")


def _pct(value) -> Decimal | None:
	"""Un porcentaje elegido, o None si no hay ninguno: Frappe guarda un Percent vacío como 0."""
	if value is None or value == "":
		return None
	try:
		pct = Decimal(str(value).strip())
	except InvalidOperation:
		raise ValueError(f"tasa de IVA no numérica: {value!r}")
	if not pct.is_finite() or pct < 0 or pct > 100:
		raise ValueError(f"tasa de IVA fuera de 0-100: {value!r}")
	return pct or None


def room_rate(room_type_pct, property_pct) -> Decimal:
	"""La tasa de una noche: la del tipo de habitación si no es 0; si no, la de la propiedad; si no, 10."""
	return _pct(room_type_pct) or _pct(property_pct) or DEFAULT_ROOM_VAT


def standard_rate(property_pct) -> Decimal:
	"""La tasa de comida, bebida y servicios: la de la propiedad si no es 0; si no, 22."""
	return _pct(property_pct) or DEFAULT_STANDARD_VAT
