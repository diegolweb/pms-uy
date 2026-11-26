"""Tasas de IVA del paquete uruguayo, sin frappe.

Los valores por defecto (habitación 10 %, comida, bebida y servicios 22 %) están pendientes del contador (HR-7): la
propiedad los cambia en sus campos `uy_iva_room_pct` y `uy_iva_standard_pct`, y un tipo de habitación con su
`tax_percent` distinto de 0 manda sobre la tasa de habitación de la propiedad.

Esa tasa de habitación es la que se ofrece a los cargos de recepción, no la de la cotización: una noche se cotiza sin
IVA para todos (`quoted_room_rate`, HR-239), porque al cotizar no se sabe quién se aloja. La reserva en línea, cuando el
huésped declara si reside en Uruguay, lleva el IVA de cada noche desde que se crea (`booking_room_rates`, HR-266,
D-HR-47): el que facturará core en el check-in, así que en el mostrador no se cobra nada si el documento lo confirma.
"""

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from kamra_uy.pure import turismo

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


def quoted_room_rate(rates_include_tax: bool, room_type_pct, property_pct) -> Decimal:
	"""El IVA con que Kamra cotiza una noche (la reserva web, recepción, su anticipo y el enlace de pago): 0.

	D-HR-40 y su aviso A6 (KICKOFF-HR-220): el precio en línea va sin IVA para todos, y el de cada noche lo decide
	core en el check-in (al extranjero, 0; al residente, 10 en las noches de temporada; al comprador con RUT, 10), que
	Kamra suma al folio (`fiscal.connector.adjust_iva`, HR-28). Cotizar el 10 hacía pagar por adelantado al extranjero
	un IVA que su factura no lleva (HR-239).

	Con `rates_include_tax` el precio configurado ya lleva el IVA y se sigue sacando a la tasa de habitación, como
	hasta ahora: el neto, y con él el precio que Kamra manda al channel manager, no cambia."""
	return room_rate(room_type_pct, property_pct) if rates_include_tax else Decimal(0)


# ── el IVA que se cobra al reservar (HR-266, D-HR-47) ──────────────────────────────────────────────────────────────
# Los dos valores del campo `uy_declared_residence` de la reserva (kamra_uy.booking.residence): lo que el huésped
# declaró en el paso de datos de la reserva en línea. Vacío = no declaró nada.
RESIDENT = "Reside en Uruguay"
NON_RESIDENT = "No reside en Uruguay"
_DECLARED = {"1": RESIDENT, "0": NON_RESIDENT, RESIDENT: RESIDENT, NON_RESIDENT: NON_RESIDENT}
CENT = Decimal("0.01")


def declared_residence(value) -> str | None:
	"""Lo que declaró el huésped: RESIDENT o NON_RESIDENT, de la página («1» o «0») o del campo. Cualquier otra cosa
	(vacío, «sí», «yes», un país…) es None: no se supone ninguna respuesta por el huésped."""
	if isinstance(value, bool):
		return RESIDENT if value else NON_RESIDENT
	if isinstance(value, int):
		value = str(value)
	if not isinstance(value, str):
		return None
	return _DECLARED.get(value.strip())


def _day(value) -> date:
	return value if isinstance(value, date) else date.fromisoformat(str(value))


def booking_room_rate(declared: str, night: date, buyer_has_rut: bool = False, room_pct=None,
                      agency_until: date | None = None) -> Decimal:
	"""El IVA de una noche como lo factura core (services/core/config/iva-rules.uy.yaml, gana la primera que encaja):

	- quien no reside en Uruguay, 0 todo el año (exportación de servicios, Decreto 220/998, art. 34, num. 14; la copia
	  del documento la toma recepción en el check-in y el hotel está en el MINTUR), también facturado a un RUT;
	- quien reside, la tasa de habitación en la alta temporada (del 16 de noviembre al domingo de Pascua, Decreto
	  267/001) y, facturado a un comprador con RUT (contribuyente de IVA), todas las noches; fuera de ella, 0;
	- salvo que el RUT sea una agencia de viajes inscripta en el MINTUR con su constancia vigente hasta `agency_until`
	  (HR-236, D-HR-74 pregunta 11; kamra_uy.fiscal.agency): sus noches hasta esa fecha, incluida, van como las de
	  quien no tiene RUT (0 fuera de la alta temporada, la tasa de habitación dentro); las de después, como un RUT más.

	`declared` es el valor del campo (RESIDENT o NON_RESIDENT). ValueError con cualquier otro: sin declaración no se
	sabe qué cobrar."""
	if declared not in (RESIDENT, NON_RESIDENT):
		raise ValueError(f"sin residencia declarada: {declared!r}")
	if declared == NON_RESIDENT:
		return Decimal(0)
	rut = buyer_has_rut and not (agency_until is not None and night <= agency_until)
	if rut or turismo.en_alta_temporada(night):
		return room_rate(None, room_pct)
	return Decimal(0)


def booking_room_rates(declared: str, llegada, salida, buyer_has_rut: bool = False, room_pct=None,
                       agency_until: date | None = None) -> dict[str, Decimal]:
	"""{AAAA-MM-DD: tasa} de cada noche de la estancia, de la llegada a la noche anterior a la salida."""
	desde, hasta = _day(llegada), _day(salida)
	out: dict[str, Decimal] = {}
	noche = desde
	while noche < hasta:
		out[noche.isoformat()] = booking_room_rate(declared, noche, buyer_has_rut, room_pct, agency_until)
		noche += timedelta(days=1)
	if not out:
		booking_room_rate(declared, desde, buyer_has_rut, room_pct, agency_until)  # sin noches, igual exige la declaración
	return out


def iva_of_nights(nights: dict, rates: dict) -> Decimal:
	"""El IVA de las noches ({fecha: importe}) a sus tasas ({fecha: tasa}), al centavo: importe × tasa / 100."""
	total = sum((Decimal(str(amount)) * Decimal(str(rates.get(day) or 0)) / 100 for day, amount in nights.items()),
	            Decimal(0))
	return total.quantize(CENT, rounding=ROUND_HALF_UP)


# ── la cotización de recepción (HR-304) ──────────────────────────────────────────────────────────────────────────
def _money(value) -> Decimal:
	return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def quote_nights(nightly: list[dict], room_total) -> dict[str, Decimal]:
	"""{AAAA-MM-DD: importe} de cada noche como la guarda la reserva que se crea con esa cotización
	(kamra Reservation.quote_night_rows): un plan de tarifa reparte el total de habitación entre las noches y la última
	se queda el redondeo."""
	free = sum((Decimal(str(n["rate"])) for n in nightly), Decimal(0))
	factor = Decimal(str(room_total or 0)) / free if free else Decimal(1)
	out: dict[str, Decimal] = {}
	running = Decimal(0)
	for i, n in enumerate(nightly):
		amount = _money(Decimal(str(n["rate"])) * factor)
		if i == len(nightly) - 1 and free:
			amount = _money(Decimal(str(room_total or 0)) - running)
		running += amount
		out[str(n["date"])] = amount
	return out


def quote_with_rates(quote: dict, rates: dict) -> dict:
	"""La cotización de Kamra (kamra.pricing.quote) con el IVA de cada noche de `rates` ({fecha: tasa}, de
	booking_room_rates), como lo deja la reserva al crearse (kamra_uy.booking.residence.apply): la diferencia de cada
	noche, importe × (tasa − la suya) / 100, entra en el IVA, el total, la seña y la línea de la habitación. Devuelve
	una copia: la cotización que llega no se toca."""
	out = {**quote, "nightly": [dict(n) for n in quote.get("nightly") or []]}
	nights = quote_nights(out["nightly"], quote.get("room_total"))
	delta = Decimal(0)
	for n in out["nightly"]:
		rate = rates.get(str(n["date"]))
		if rate is None:
			continue
		delta += nights[str(n["date"])] * (Decimal(str(rate)) - Decimal(str(n.get("gst_rate") or 0))) / 100
		n["gst_rate"] = float(rate)
	delta = _money(delta)
	base = Decimal(str(quote.get("amount_before_tax") or 0))
	tax = _money(quote.get("tax_amount")) + delta
	total = _money(base + tax)
	out.update(tax_amount=float(tax), amount_after_tax=float(total),
	           tax_percent=round(float(tax / base * 100), 2) if base else 0.0)
	if quote.get("totals"):
		out["totals"] = {**quote["totals"], "tax": float(tax), "total": float(total)}
	if quote.get("advance"):
		pct = Decimal(str(quote["advance"].get("percent") or 0))
		out["advance"] = {**quote["advance"], "amount": float(_money(total * pct / 100))}
	if quote.get("line_items"):
		items = [dict(item) for item in quote["line_items"]]
		room = next((item for item in items if item.get("component") == "accommodation"), None)
		if room is not None:
			room_tax = _money(room.get("tax_amount")) + delta
			room_amount = Decimal(str(room.get("amount") or 0))
			room.update(tax_amount=float(room_tax),
			            tax_rate=round(float(room_tax / room_amount * 100), 2) if room_amount else 0.0)
		out["line_items"] = items
	return out
