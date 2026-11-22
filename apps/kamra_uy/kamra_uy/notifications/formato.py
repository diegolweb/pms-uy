"""Importes, fechas y horas del correo, como los pinta la web (web/src/booking/reglas.ts). Sin frappe.

El correo y la página de confirmada dicen lo mismo con las mismas cifras: «US$ 1.265,50» (es, pt) o «US$ 1,265.50»
(en), con los decimales solo si no es entero (D-S53-3); y las fechas como `fechaCorta`, «Vie 8 ene 2027».
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from kamra_uy.notifications.texts import DIAS, MESES

PREFIJO = {"USD": "US$ ", "UYU": "$ "}
SEPARADORES = {"es": (".", ","), "pt": (".", ","), "en": (",", ".")}  # (miles, decimales)


def importe(valor, moneda: str, idioma: str) -> str:
	"""Un importe de Kamra tal cual, con el prefijo de su moneda (o su código); los decimales solo si no es entero."""
	miles, decimales = SEPARADORES[idioma]
	centimos = int((abs(Decimal(str(valor or 0))) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
	entero, resto = divmod(centimos, 100)
	cifra = f"{entero:,}".replace(",", miles) + (f"{decimales}{resto:02d}" if resto else "")
	signo = "-" if float(valor or 0) < 0 and centimos else ""
	return f"{PREFIJO.get(moneda, f'{moneda} ')}{signo}{cifra}"


def _dia(valor) -> date:
	if isinstance(valor, datetime):
		return valor.date()
	if isinstance(valor, date):
		return valor
	return date.fromisoformat(str(valor))


def fecha_corta(valor, idioma: str) -> str:
	"""«Vie 8 ene 2027»: el día de la semana, el día, el mes y el año, con las tablas de la web."""
	dia = _dia(valor)
	return f"{DIAS[idioma][(dia.weekday() + 1) % 7]} {dia.day} {MESES[idioma][dia.month - 1]} {dia.year}"


def hora(valor) -> str:
	"""«14:00» de un Time de Frappe (str «14:00:00», timedelta o time); "" si no hay."""
	if valor in (None, ""):
		return ""
	if isinstance(valor, timedelta):
		minutos = int(valor.total_seconds()) // 60
		return f"{minutos // 60 % 24:02d}:{minutos % 60:02d}"
	if isinstance(valor, time):
		return f"{valor.hour:02d}:{valor.minute:02d}"
	partes = str(valor).split(":")
	return f"{int(partes[0]):02d}:{int(partes[1]):02d}"
