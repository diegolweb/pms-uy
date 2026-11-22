"""No vender antes de la apertura (HR-263, D-HR-39). Sin frappe.

La propiedad lleva su fecha de apertura (`uy_opening_date`, campo de kamra_uy: kamra_uy.booking.opening). La reserva en
línea no vende una estancia cuya LLEGADA sea anterior a esa fecha; la del mismo día o posterior, sí. Sin fecha (el campo
vacío, como nace), no hay restricción. Una llegada que no es una fecha no se juzga aquí: la rechazan la búsqueda o la
reserva de Kamra con su propio motivo.

El motivo del rechazo es la misma línea que la web pinta en el paso 1 cuando la apertura es futura (`fechas.apertura`
de web/content/{es,pt,en}/reservar.md, con la fecha como `fechaCorta`): lo ata apps/kamra_uy/tests/unit/test_opening.py.
Es un BORRADOR hasta el OK del fundador (D-HR-44). Español neutro, portugués de Brasil con «você», inglés internacional.

Las plataformas tampoco venden antes de la apertura (HR-264): con la apertura en el futuro, las noches de HOY a la
VÍSPERA quedan cerradas (`cierre`) en una Channel Restriction stop sell de la propiedad que mantiene kamra_uy
(kamra_uy.booking.stop_sell) y que el outbox de ARI de Kamra manda a Channex. La primera noche que se vende es la de la
apertura: la misma llegada que acepta el guardián. Hoy es el más temprano de los dos relojes (`hoy`: el del sitio, el
de las celdas de ARI, y el del hotel): así ninguna noche que alguno de los dos aún vende queda fuera. La fila es de
kamra_uy por su nombre (`nombre_cierre`, determinista, con el prefijo `PREFIJO_CIERRE`); las de la pantalla de
restricciones nacen con el nombre al azar de Kamra y nunca se tocan. Su nota y el aviso al editarla a mano los lee
quien gestiona las restricciones en el escritorio: van solo en español, como los campos de kamra_uy, y son BORRADOR.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta

from kamra_uy.notifications.formato import fecha_corta

TEXTOS = {
	"es": "Apertura del hotel: {fecha}. Puedes reservar estadías con llegada desde esa fecha.",
	"pt": "Abertura do hotel: {fecha}. Você pode reservar estadias com chegada a partir dessa data.",
	"en": "Hotel opening: {fecha}. You can book stays arriving from that date.",
}
PREFIJO_CIERRE = "UY-APERTURA-"
NOTA_CIERRE = ("Cierre de venta hasta la apertura del hotel. Lo mantiene la fecha de apertura de la propiedad: se cambia "
               "o se quita desde ahí.")
CIERRE_BLOQUEADO = ("Esta restricción la mantiene la fecha de apertura del hotel: se cambia o se quita desde la fecha de "
                    "apertura de la propiedad.")
_DIA = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[ T][\d:.]+)?$")


def fecha(valor) -> date | None:
	"""La fecha de un Date de Frappe (date, datetime o «AAAA-MM-DD», con hora o sin ella); None si no hay o no lo es."""
	if isinstance(valor, datetime):
		return valor.date()
	if isinstance(valor, date):
		return valor
	if not isinstance(valor, str):
		return None
	m = _DIA.match(valor.strip())
	if not m:
		return None
	try:
		return date.fromisoformat(m.group(1))
	except ValueError:
		return None


def antes_de_abrir(llegada, apertura) -> bool:
	"""¿La llegada es anterior a la apertura? Sin apertura, o con una llegada que no es fecha, no."""
	a, ll = fecha(apertura), fecha(llegada)
	return bool(a and ll and ll < a)


def motivo(apertura, idioma: str | None) -> str:
	"""La línea de la apertura en el idioma de la página (es, pt o en; con otro, español)."""
	a = fecha(apertura)
	if not a:
		raise ValueError("motivo: sin fecha de apertura")
	lengua = idioma if idioma in TEXTOS else "es"
	return TEXTOS[lengua].format(fecha=fecha_corta(a, lengua))


def hoy(sitio, propiedad) -> date | None:
	"""El más temprano de los dos relojes (el del sitio y el del hotel); el que haya si falta uno; None sin ninguno."""
	dias = [d for d in (fecha(sitio), fecha(propiedad)) if d]
	return min(dias) if dias else None


def cierre(hoy, apertura) -> tuple[date, date] | None:
	"""Las noches que las plataformas no venden: (hoy, víspera de la apertura), ambas incluidas. None sin apertura, sin
	hoy, o si la apertura ya llegó (hoy es ese día o después)."""
	a, h = fecha(apertura), fecha(hoy)
	if not a or not h or h >= a:
		return None
	return h, a - timedelta(days=1)


def nombre_cierre(propiedad: str) -> str:
	"""El name de la Channel Restriction de kamra_uy de una propiedad: el prefijo y 16 caracteres hexadecimales de su sha1 (cabe en un
	name de Frappe y no lleva caracteres que Frappe rechace, sea cual sea el nombre de la propiedad)."""
	return PREFIJO_CIERRE + hashlib.sha1(str(propiedad).encode("utf-8")).hexdigest()[:16].upper()


def es_cierre(nombre) -> bool:
	"""¿Es una fila de kamra_uy? Por el prefijo, sin distinguir mayúsculas (MariaDB tampoco)."""
	return isinstance(nombre, str) and nombre.upper().startswith(PREFIJO_CIERRE)
