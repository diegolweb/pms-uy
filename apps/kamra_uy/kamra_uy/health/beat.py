"""El latido del programador (HR-21): un trabajo del programador de Frappe escribe la hora en un fichero de la carpeta
del sitio. Lo arranca el programador desde hooks.py (scheduler_events, cron «* * * * *», que Frappe 16 encola una vez
por tick: cada 4 min por defecto) y lo corre un trabajador: si se para cualquiera de los dos, el latido envejece y la
señal `programador` de kamra_uy/health/checks.py se pone roja.

Va a un fichero de la carpeta del sitio (que comparten el backend y el worker) y no a la base ni a la caché: escribir
un valor global en la base en cada tick vaciaría la caché de arranque de todos los usuarios, y la de redis se pierde si
redis se reinicia (una falsa alarma). Fuera de las copias (`bench backup` no lo lleva): tras restaurar, el primer latido
lo vuelve a crear.
"""

from __future__ import annotations

import os
from datetime import datetime

import frappe
from frappe.utils import get_datetime, now_datetime

FILE = "kamra_uy_latido"


def path() -> str:
	return frappe.get_site_path(FILE)


def _write(value: str) -> None:
	target = path()
	tmp = f"{target}.{os.getpid()}.tmp"
	with open(tmp, "w") as f:
		f.write(value)
	os.replace(tmp, target)


def heartbeat() -> None:
	"""El trabajo de cada minuto: la hora del sitio (la de now_datetime) en el fichero."""
	_write(now_datetime().isoformat())


def last() -> datetime | None:
	"""La hora del último latido, o None si nunca hubo uno (o el fichero no se entiende)."""
	try:
		with open(path()) as f:
			return get_datetime(f.read().strip())
	except (OSError, ValueError, TypeError):
		return None
