"""Las reglas del formulario de contacto de la web del hotel (HR-314), sin frappe.

La página /contacto (y su pt y en) manda a `kamra_uy.contact.form.send` el nombre, el apellido, el email, el teléfono
(opcional, con su código de país) y el mensaje; además, el idioma de la página (`_lang`), el campo trampa (`sitio`, que
una persona nunca ve) y los milisegundos desde que se cargó la página (`espera`). Lo que valida el navegador no vale
nada: aquí se vuelve a validar todo y se limpia lo que va al correo de recepción.

- Un campo de una línea (nombre, apellido, email, teléfono) llega sin saltos ni caracteres de control: nada que pueda
  partir el asunto o añadir una cabecera al correo. El email, además, es UNA dirección ASCII sin comas, espacios ni
  ángulos: va en el Reply-To.
- El mensaje conserva sus saltos de línea y sus tabuladores, sin otros caracteres de control.
- Contra el spam, sin terceros: el campo trampa lleno, o menos de 3 s desde la carga (o ninguna medida), es spam
  (`Result.spam`) y no dice qué campo está mal.
"""

from __future__ import annotations

import dataclasses
import math
import re
import unicodedata

FIELDS = ("nombre", "apellido", "email", "telefono", "mensaje")
REQUIRED = ("nombre", "apellido", "email", "mensaje")
LANGUAGES = ("es", "pt", "en")
MAX_LENGTH = {"nombre": 80, "apellido": 80, "email": 254, "telefono": 30, "mensaje": 4000}
MIN_WAIT_MS = 3000
TRAP = "sitio"
WAIT = "espera"
# los códigos que devuelve el servidor por campo; la web los traduce (content/<idioma>/contacto.md)
ERROR_CODES = ("requerido", "email", "telefono", "largo")

_EMAIL = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
                    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")
_PHONE_CHARS = re.compile(r"^[0-9 ().+-]+$")
_PHONE_DIGITS = (8, 15)  # E.164: hasta 15 cifras con el código de país


@dataclasses.dataclass(frozen=True)
class Result:
	ok: bool
	data: dict
	errors: dict
	spam: str | None = None  # «trampa» o «rapido»


def _sin_control(texto: str, conserva: str = "") -> str:
	"""Fuera los caracteres de control y de formato (Cc, Cf), salvo los de `conserva`."""
	return "".join(c for c in texto if c in conserva or unicodedata.category(c) not in ("Cc", "Cf"))


def _una_linea(valor) -> str:
	return _sin_control(" ".join(str(valor or "").split()))


def _mensaje(valor) -> str:
	texto = str(valor or "").replace("\r\n", "\n").replace("\r", "\n")
	return _sin_control(texto, conserva="\n\t").strip()


def is_email(valor: str) -> bool:
	return bool(_EMAIL.match(valor or "")) and ".." not in valor


def clean_phone(valor: str) -> str | None:
	"""El teléfono con su código de país («+598 99 123 456»; «00598…» pasa a «+598…»), o None si no lo es."""
	texto = (valor or "").strip()
	if texto.startswith("00"):
		texto = "+" + texto[2:]
	if not _PHONE_CHARS.match(texto) or not re.match(r"^\+[0-9(]", texto) or texto.count("+") != 1:
		return None
	cifras = sum(c.isdigit() for c in texto)
	return texto if _PHONE_DIGITS[0] <= cifras <= _PHONE_DIGITS[1] else None


def language(valor) -> str:
	base = re.split(r"[-_]", str(valor or "").strip().lower(), maxsplit=1)[0]
	return base if base in LANGUAGES else LANGUAGES[0]


def _too_fast(valor) -> bool:
	try:
		ms = float(valor)
	except (TypeError, ValueError):
		return True
	return not math.isfinite(ms) or ms < MIN_WAIT_MS


def check(form: dict) -> Result:
	"""Valida y limpia lo que manda la web. `Result.ok` solo si no es spam y no hay ningún error."""
	if str(form.get(TRAP) or "").strip():
		return Result(ok=False, data={}, errors={}, spam="trampa")
	if _too_fast(form.get(WAIT)):
		return Result(ok=False, data={}, errors={}, spam="rapido")
	data = {campo: (_mensaje if campo == "mensaje" else _una_linea)(form.get(campo)) for campo in FIELDS}
	errors = {}
	for campo in FIELDS:
		valor = data[campo]
		if not valor:
			if campo in REQUIRED:
				errors[campo] = "requerido"
		elif len(valor) > MAX_LENGTH[campo]:
			errors[campo] = "largo"
		elif campo == "email" and not is_email(valor):
			errors[campo] = "email"
		elif campo == "telefono":
			telefono = clean_phone(valor)
			if telefono is None:
				errors[campo] = "telefono"
			else:
				data[campo] = telefono
	data["idioma"] = language(form.get("_lang"))
	return Result(ok=not errors, data=data, errors=errors)
