"""Cotizaciones del Banco Central del Uruguay (servicio SOAP público `awsbcucotizaciones`), sin red y sin frappe.

El BCU cotiza cada moneda en pesos uruguayos por unidad extranjera, con un tipo comprador (TCC) y uno vendedor (TCV)
por día hábil. Las monedas se identifican por su código numérico del BCU, NUNCA por su `CodigoISO`: el euro llega como
«EURO» y el dólar billete como «DLS.».

Dos usos:
- La carga de cambio de Kamra (`rates_for_home`, la caja de recepción): código, lado y día son parámetros
  (`site_config` `kamra_uy_bcu`).
- La cotización de la FACTURA (`fiscal_quote`, HR-348): la fija el estudio contable (D-HR-74, respuesta 7) y no se
  configura: dólar interbancario comprador billete (serie 2225, TCC) del último día hábil del BCU ANTERIOR al día de la
  operación. Es la que toma Gsoft; nunca se teclea.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree
from xml.sax.saxutils import escape

BCU_URL = "https://cotizaciones.bcu.gub.uy/wscotizaciones/servlet/awsbcucotizaciones"
SOAP_ACTION = "Cotizaaction/AWSBCUCOTIZACIONES.Execute"
NAMESPACE = "Cotiza"
HOME = "UYU"
# 2225 «DLS. USA BILLETE», el dólar interbancario billete que pide el estudio (D-HR-74; 2222 «DOLAR USA» y 2224
# «DLS. USA CABLE» son otras series del dólar), 500 PESO ARGENTINO, 1000 REAL, 1111 EURO
DEFAULT_CODES = {"USD": 2225, "ARS": 500, "BRL": 1000, "EUR": 1111}
SIDES = ("TCC", "TCV")
# la cotización de la factura (D-HR-74, respuesta 7): solo el dólar, la otra moneda que admite el CFE además del peso
FISCAL_CODES = {"USD": 2225}
FISCAL_SIDE = "TCC"
# cuántos días hacia atrás se piden: cubre la semana de Turismo con sus dos fines de semana
FISCAL_LOOKBACK_DAYS = 15
# desde esta hora del día de la operación, un día hábil sin cotización es un feriado del BCU y no una que aún no publicó
# (el 24-11-2026 a las 16:42 todavía no estaba la del día)
SETTLED_HOUR = 12
_NINE = Decimal("1e-9")


@dataclass(frozen=True)
class BcuQuote:
	day: date
	code: int
	tcc: Decimal
	tcv: Decimal


def request_body(codes: Iterable[int], since: date, until: date, group: int = 0) -> bytes:
	"""El sobre SOAP que pide las cotizaciones de `codes` entre dos fechas (grupo 0: todas las cotizaciones)."""
	items = "".join(f"<cot:item>{int(code)}</cot:item>" for code in codes)
	return (
		'<?xml version="1.0" encoding="utf-8"?>'
		'<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
		f'xmlns:cot="{escape(NAMESPACE)}"><soapenv:Header/><soapenv:Body>'
		"<cot:wsbcucotizaciones.Execute><cot:Entrada>"
		f"<cot:Moneda>{items}</cot:Moneda>"
		f"<cot:FechaDesde>{since.isoformat()}</cot:FechaDesde>"
		f"<cot:FechaHasta>{until.isoformat()}</cot:FechaHasta>"
		f"<cot:Grupo>{int(group)}</cot:Grupo>"
		"</cot:Entrada></cot:wsbcucotizaciones.Execute></soapenv:Body></soapenv:Envelope>"
	).encode()


def _local(tag: str) -> str:
	return tag.rsplit("}", 1)[-1]


def _child(node, name: str):
	for child in node:
		if _local(child.tag) == name:
			return child
	return None


def _find(node, name: str):
	for child in node.iter():
		if _local(child.tag) == name:
			return child
	return None


def _rate(dato, name: str) -> Decimal:
	node = _child(dato, name)
	try:
		value = Decimal((node.text or "").strip()) if node is not None else None
	except InvalidOperation:
		value = None
	if value is None or not value.is_finite() or value <= 0:
		raise ValueError(f"cotización {name} no válida en la respuesta del BCU")
	return value


def _quote(dato) -> BcuQuote:
	fecha, moneda = _child(dato, "Fecha"), _child(dato, "Moneda")
	if fecha is None or moneda is None:
		raise ValueError("cotización del BCU sin Fecha o sin Moneda")
	try:
		day = date.fromisoformat((fecha.text or "").strip())
		code = int((moneda.text or "").strip())
	except ValueError:
		raise ValueError("Fecha o Moneda no válidas en la respuesta del BCU")
	return BcuQuote(day, code, _rate(dato, "TCC"), _rate(dato, "TCV"))


def parse_cotizaciones(xml: bytes) -> list[BcuQuote]:
	"""Las cotizaciones de una respuesta del BCU. ValueError si no es la respuesta esperada o su `status` no es 1.
	La respuesta es ajena: se rechaza cualquier DTD (entidades) antes de leerla. Para que la búsqueda vea lo mismo que
	el parser, solo se admite UTF-8 (con o sin BOM) y sin NUL: un UTF-16 sin BOM son bytes UTF-8 válidos con NUL, y
	expat lo autodetectaría como UTF-16. Se parsea el texto ya decodificado, no los bytes."""
	try:
		text = xml.decode("utf-8-sig")
	except UnicodeDecodeError:
		raise ValueError("la respuesta del BCU no viene en UTF-8")
	if "\x00" in text:
		raise ValueError("la respuesta del BCU no viene en UTF-8 (trae NUL)")
	if "<!DOCTYPE" in text or "<!ENTITY" in text:
		raise ValueError("la respuesta del BCU trae una DTD")
	try:
		root = ElementTree.fromstring(text)
	except ElementTree.ParseError as e:
		raise ValueError(f"la respuesta del BCU no es XML: {e}")
	status = _find(root, "respuestastatus")
	if status is None:
		raise ValueError("la respuesta del BCU no trae respuestastatus")
	status_node = _child(status, "status")
	code = (status_node.text or "").strip() if status_node is not None else ""
	if code != "1":
		message = _child(status, "mensaje")
		detail = (message.text or "").strip() if message is not None else ""
		raise ValueError(f"el BCU respondió status {code or '?'}: {detail}")
	datos = _find(root, "datoscotizaciones")
	if datos is None:
		raise ValueError("la respuesta del BCU no trae datoscotizaciones")
	return [_quote(dato) for dato in datos if _local(dato.tag) == "datoscotizaciones.dato"]


def _side(side: str) -> str:
	side = (side or "").strip().upper()
	if side not in SIDES:
		raise ValueError(f"lado de cotización desconocido: {side!r} (TCC comprador o TCV vendedor)")
	return side


def _latest(quotes: Iterable[BcuQuote], code: int, cutoff: date) -> BcuQuote | None:
	eligible = [q for q in quotes if q.code == code and q.day <= cutoff]
	return max(eligible, key=lambda q: q.day) if eligible else None


def rates_for_home(quotes: list[BcuQuote], home: str, wanted: list[str], codes: dict[str, int], side: str,
                   on: date, day_offset: int = 1) -> dict[str, Decimal]:
	"""Unidades de `home` por 1 unidad de cada moneda de `wanted`, con la cotización más reciente de fecha
	≤ `on` - `day_offset` (fines de semana y feriados: la última anterior), a 9 decimales. Con una casa distinta del
	peso, el tipo es cruzado: (pesos por extranjera) / (pesos por casa). Lo que no tiene cotización no aparece."""
	side = _side(side)
	home = (home or "").strip().upper()
	codes = {k.strip().upper(): int(v) for k, v in codes.items()}
	cutoff = on - timedelta(days=day_offset)

	def pesos(currency: str) -> Decimal | None:
		if currency == HOME:
			return Decimal(1)
		code = codes.get(currency)
		quote = _latest(quotes, code, cutoff) if code is not None else None
		return getattr(quote, side.lower()) if quote else None

	home_pesos = pesos(home)
	if home_pesos is None:
		return {}
	out = {}
	for currency in (c.strip().upper() for c in wanted):
		if currency == home:
			continue
		value = pesos(currency)
		if value is not None:
			out[currency] = (value / home_pesos).quantize(_NINE)
	return out


def as_of(quotes: Iterable[BcuQuote], on: date, day_offset: int = 1) -> date | None:
	"""El día de la cotización más reciente que `rates_for_home` puede usar con esos mismos `on` y `day_offset`."""
	cutoff = on - timedelta(days=day_offset)
	days = [q.day for q in quotes if q.day <= cutoff]
	return max(days) if days else None


def fiscal_window(on: date) -> tuple[date, date]:
	"""Las fechas que se piden al BCU para la factura de una operación del día `on`: hasta el día anterior."""
	return on - timedelta(days=FISCAL_LOOKBACK_DAYS), on - timedelta(days=1)


def fiscal_quote(quotes: Iterable[BcuQuote], code: int, on: date, now: datetime) -> BcuQuote | None:
	"""La cotización con que se factura una operación del día `on` (D-HR-74): la del último día hábil del BCU ANTERIOR a
	`on`. `now` es la hora local de la propiedad. None mientras no se sepa cuál es: sin ninguna anterior, o si entre ella y
	`on` queda un día de semana sin cotización y todavía no son las SETTLED_HOUR del día de la operación (puede ser un
	feriado o una que el BCU aún no publicó; los sábados y domingos no cotiza nunca)."""
	before = [q for q in quotes if q.code == code and q.day < on]
	if not before:
		return None
	last = max(before, key=lambda q: q.day)
	gap = [last.day + timedelta(days=k) for k in range(1, (on - last.day).days)]
	if all(day.weekday() >= 5 for day in gap) or now >= datetime.combine(on, time(SETTLED_HOUR)):
		return last
	return None
