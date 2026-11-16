"""Importe en letras en español, como lo escribe una factura uruguaya: «pesos uruguayos mil doscientos cincuenta con
50/100». Sin frappe. Los centavos se redondean a dos decimales con ROUND_HALF_UP y van en cifras sobre 100.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CURRENCY_NAMES = {"UYU": "pesos uruguayos", "USD": "dólares estadounidenses"}

_UNITS = ["cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez", "once", "doce",
          "trece", "catorce", "quince", "dieciséis", "diecisiete", "dieciocho", "diecinueve", "veinte", "veintiuno",
          "veintidós", "veintitrés", "veinticuatro", "veinticinco", "veintiséis", "veintisiete", "veintiocho",
          "veintinueve"]
_TENS = {3: "treinta", 4: "cuarenta", 5: "cincuenta", 6: "sesenta", 7: "setenta", 8: "ochenta", 9: "noventa"}
_HUNDREDS = {1: "ciento", 2: "doscientos", 3: "trescientos", 4: "cuatrocientos", 5: "quinientos", 6: "seiscientos",
             7: "setecientos", 8: "ochocientos", 9: "novecientos"}
_MILLION = 10**6
_BILLION = 10**12  # el billón español: un millón de millones
_LIMIT = 10**18  # un trillón: ningún importe de hotel llega


def _below_thousand(n: int) -> str:
	if n < 30:
		return _UNITS[n]
	if n < 100:
		tens, unit = divmod(n, 10)
		return _TENS[tens] + (f" y {_UNITS[unit]}" if unit else "")
	if n == 100:
		return "cien"
	hundreds, rest = divmod(n, 100)
	return _HUNDREDS[hundreds] + (f" {_below_thousand(rest)}" if rest else "")


def _apocope(words: str) -> str:
	"""«uno» ante «mil», «millón» o «billón» se apocopa: veintiún mil, treinta y un millones."""
	if words.endswith("veintiuno"):
		return words[: -len("veintiuno")] + "veintiún"
	if words == "uno" or words.endswith(" uno"):
		return words[: -len("uno")] + "un"
	return words


def _below_million(n: int) -> str:
	thousands, rest = divmod(n, 1000)
	if not thousands:
		return _below_thousand(rest)
	head = "mil" if thousands == 1 else f"{_apocope(_below_thousand(thousands))} mil"
	return head + (f" {_below_thousand(rest)}" if rest else "")


def _scaled(n: int, scale: int, one: str, many: str, below) -> str:
	count, rest = divmod(n, scale)
	if not count:
		return below(rest)
	head = f"un {one}" if count == 1 else f"{_apocope(_below_million(count))} {many}"
	return head + (f" {below(rest)}" if rest else "")


def _below_billion(n: int) -> str:
	return _scaled(n, _MILLION, "millón", "millones", _below_million)


def integer_words(n: int) -> str:
	"""Un entero de 0 a 10**18 - 1 en letras: 21 → «veintiuno», 1_000_001 → «un millón uno»."""
	if not 0 <= n < _LIMIT:
		raise ValueError(f"fuera de rango para escribir en letras: {n}")
	return _scaled(n, _BILLION, "billón", "billones", _below_billion)


def amount_in_words(amount, currency: str) -> str:
	"""«<moneda> <entero en letras> con <cc>/100». Una moneda sin nombre conocido lleva su código en mayúsculas."""
	try:
		value = Decimal(str(amount).strip())
	except InvalidOperation:
		raise ValueError(f"importe no numérico: {amount!r}")
	if not value.is_finite() or value < 0:
		raise ValueError(f"importe que no se escribe en letras: {amount!r}")
	cents_total = int(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)
	whole, cents = divmod(cents_total, 100)
	code = (currency or "").strip().upper()
	name = CURRENCY_NAMES.get(code, code)
	return f"{name} {integer_words(whole)} con {cents:02d}/100".strip()
