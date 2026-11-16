"""Uruguay: IVA, pesos uruguayos, vocabulario de recepción y tipo de cambio del BCU para Kamra.

Lo resuelve `kamra.localization.pack_for_country("Uruguay")` porque kamra_uy declara el hook `kamra_localization`
(hooks.py) y se instala después de kamra.

Qué cubre:
- IVA de cotización con la tasa de RESIDENTE: habitación 10 % y comida, bebida y servicios 22 %, configurables por
  propiedad (`Property.uy_iva_room_pct` y `Property.uy_iva_standard_pct`). Un tipo de habitación con `tax_percent`
  distinto de 0 manda sobre la tasa de habitación. Las tasas de la India de Kamra (`gst_rate_low`/`gst_rate_high`) no
  se leen nunca: una propiedad nueva cotizaría al 5 % sin que nadie lo notara.
- Etiquetas de la cuenta («IVA», «RUT»), su título y su pie, y el importe en letras en español.
- Documentos (cédula como «National ID», DNI argentino, RG brasileño, pasaporte) y modos de pago «Tarjeta local» y
  «Tarjeta extranjera», que van en INSTANT_PAYMENT_MODES porque `localization.payment_modes` solo deja pasar los
  genéricos y los instantáneos; fixtures/property_setter.json los añade a los Select de Kamra que guardan el modo.
- `market_rates(home)`: las cotizaciones del BCU para la carga de cambio de Kamra (`ledger._fetch_rates`), con su
  margen, su respaldo manual y su caché de siempre. Qué código, qué lado (TCC/TCV) y qué día usa se configura en
  `site_config.json`, clave `kamra_uy_bcu` = {"codes": {...}, "side": "TCC", "day_offset": 1}, sin tocar código.

Qué NO cubre (otras fichas): el IVA 0 a no residentes, que decide el motor de IVA al facturar con el país de residencia
del huésped (`Guest.residence_country`) y el modo de pago; el CFE y su envío a DGI; Mercado Pago.

Tasas y cotización pendientes del contador (HR-7).
"""

from datetime import timedelta
from decimal import Decimal

import frappe
from frappe.utils import getdate, nowdate

from kamra_uy.pure import bcu, uy_tax, words_es

DEFAULT_CURRENCY = "UYU"
# las monedas que más traen los huéspedes; Settings, Finance, Exchange rates las cambia
FX_CURRENCIES = ["USD", "ARS", "BRL", "EUR"]
DEFAULT_TIMEZONE = "America/Montevideo"
DEFAULT_NATIONALITY = "Uruguayan"
# el código de país que usa la MRZ de un pasaporte o una cédula uruguaya
HOME_COUNTRY_ISO3 = "URY"
# «National ID» es la cédula de identidad uruguaya; DNI argentino y RG brasileño; Passport lo usa id_scan
ID_TYPES = ["National ID", "DNI", "RG", "Passport", "Other"]
PAYMENT_MODES = ["Cash", "Tarjeta local", "Tarjeta extranjera", "Bank Transfer"]
# el origen de la tarjeta lo lee el motor de IVA al facturar; la caja las suma en su cajón de instantáneos
INSTANT_PAYMENT_MODES = ["Tarjeta local", "Tarjeta extranjera"]
NOTICE_LANGUAGES = ["es", "pt", "en"]
# borrador para el OK del fundador: hasta el CFE, este papel no debe parecer una factura fiscal
INVOICE_TITLE = "Detalle de cuenta"
INVOICE_FOOTER = ("Este documento no es un comprobante fiscal. El comprobante fiscal electrónico (CFE) se emite por "
                  "separado.")
CURRENCY_SYMBOLS = {"UYU": "$", "USD": "US$"}
BCU_LOOKBACK_DAYS = 10
FX_FALLBACK = "Today's rates are unchanged; type them in on the Currency Desk."


def _property_rates(property) -> dict:
	if not property:
		return {}
	return frappe.db.get_value("Property", property, ["uy_iva_room_pct", "uy_iva_standard_pct"], as_dict=True) or {}


def _number(value: Decimal):
	return int(value) if value == value.to_integral_value() else float(value)


def calculate_room_tax(property, room_type_doc, nightly_rate) -> Decimal:
	room_type_pct = room_type_doc.get("tax_percent") if room_type_doc else None
	return uy_tax.room_rate(room_type_pct, _property_rates(property).get("uy_iva_room_pct"))


def fnb_tax_rate(property) -> float:
	return float(uy_tax.standard_rate(_property_rates(property).get("uy_iva_standard_pct")))


def services_tax_rate(property) -> float:
	return fnb_tax_rate(property)


def tax_rate_options(property) -> list:
	rates = _property_rates(property)
	room = uy_tax.room_rate(None, rates.get("uy_iva_room_pct"))
	standard = uy_tax.standard_rate(rates.get("uy_iva_standard_pct"))
	return [_number(r) for r in sorted({Decimal(0), room, standard})]


def invoice_context(prop_doc) -> dict:
	return {
		"tax_label": "IVA",
		"tax_id_label": "RUT",
		"service_code": None,
		"sac": None,
		"place_of_supply": None,
		"split": [("iva", Decimal("1"))],
		"footer": INVOICE_FOOTER,
	}


def _currency(prop_doc) -> str:
	return (prop_doc.get("currency") or DEFAULT_CURRENCY).strip().upper()


def locale(prop_doc) -> dict:
	currency = _currency(prop_doc)
	symbol = CURRENCY_SYMBOLS.get(currency)
	if not symbol:
		symbol = (frappe.db.get_value("Currency", currency, "symbol") or "").strip() or f"{currency} "
	return {
		"currency_symbol": symbol,
		"locale": "es-UY",
		"currency": currency,
		"tax_label": "IVA",
		"tax_id_label": "RUT",
		"tax_rates": tax_rate_options(prop_doc.get("name")),
	}


def amount_in_words(prop_doc, amount) -> str:
	return words_es.amount_in_words(Decimal(str(amount or 0)), _currency(prop_doc))


def _bcu_config() -> tuple[dict[str, int], str, int]:
	conf = frappe.conf.get("kamra_uy_bcu") or {}
	codes = {str(k).upper(): int(v) for k, v in (conf.get("codes") or bcu.DEFAULT_CODES).items()}
	side = str(conf.get("side") or "TCC").upper()
	day_offset = int(conf.get("day_offset", 1))
	if day_offset < 0:
		raise ValueError("kamra_uy_bcu.day_offset no puede ser negativo")
	return codes, side, day_offset


def market_rates(home: str) -> tuple[dict, str | None]:
	"""Las cotizaciones del BCU como unidades de `home` por 1 unidad extranjera, y la fecha de la cotización usada.
	Lo llama `kamra.ledger` en vez de su proveedor abierto. La respuesta es ajena: con tope de tiempo y de tamaño, sin
	redirecciones; ante cualquier fallo, el mismo aviso de respaldo manual de Kamra."""
	import requests

	from kamra.ledger import FX_MAX_BYTES, FX_TIMEOUT_S

	try:
		home = (home or "").strip().upper()
		if not (len(home) == 3 and home.isalpha()):
			raise ValueError(f"home currency {home!r} is not an ISO code")
		codes, side, day_offset = _bcu_config()
		today = getdate(nowdate())
		body = bcu.request_body(sorted(set(codes.values())), today - timedelta(days=BCU_LOOKBACK_DAYS), today)
		r = requests.post(bcu.BCU_URL, data=body, timeout=FX_TIMEOUT_S, stream=True, allow_redirects=False,
		                  headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": bcu.SOAP_ACTION})
		r.raise_for_status()
		reply = r.raw.read(FX_MAX_BYTES + 1, decode_content=True)
		if len(reply) > FX_MAX_BYTES:
			raise ValueError("reply too large")
		quotes = bcu.parse_cotizaciones(reply)
		wanted = [*codes, bcu.HOME]
		rates = bcu.rates_for_home(quotes, home, wanted, codes, side, today, day_offset)
		if not rates:
			raise ValueError(f"el BCU no trae cotizaciones para {home}")
		as_of = bcu.as_of(quotes, today, day_offset)
	except Exception as e:
		frappe.throw(f"Could not fetch exchange rates ({type(e).__name__}: {e}). {FX_FALLBACK}")
	return {currency: float(rate) for currency, rate in rates.items()}, (as_of.isoformat() if as_of else None)
