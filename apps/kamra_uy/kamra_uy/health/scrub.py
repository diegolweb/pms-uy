"""Lo que sale hacia Sentry, sin datos personales (HR-21; R-S1-10 del kickoff temporada-dic).

Dos piezas, sin frappe (las usa kamra_uy/health/sentry.py y se prueban fuera del bench):

- `text(s)`: un texto libre (el mensaje de un error, su traza, el título de un Error Log) con los correos, los números
  largos (teléfonos, cédulas, RUT, tarjetas, fechas), lo que va entre comillas o en negrita (nombres) y lo que sigue a
  `token=`, `password:`… cambiados por una marca. Se quedan los números cortos (líneas de la traza, noches, %), las
  rutas de fichero y los nombres con punto o guion bajo (`kamra.api.book`, `room_type`), que es lo que sirve para
  arreglar el error.
- `traceback(s)`, `title(t, traza)` y `message(s)`: una traza en texto con solo su estructura (ficheros, líneas,
  código y el TIPO de cada excepción, nunca su mensaje: es texto libre y puede llevar un nombre sin comillas, como
  «Guest Juana Pérez not found»), y el título de un Error Log (si es el mensaje de la excepción, su tipo).
- `event(ev, hint)`: el `before_send` de sentry-sdk. Arma un evento NUEVO solo con lo que está en la lista (tipo y
  traza del error sin su mensaje ni sus variables locales, el mensaje filtrado, la versión, el servidor, el entorno y
  la huella), así
  que lo que Frappe o sentry-sdk añadan mañana no sale: ni el usuario, ni el cuerpo de la petición (Frappe lo manda en
  los contextos «JSON Body» y «Form Data»), ni las cookies ni las cabeceras, ni las migas (llevan el SQL con sus
  valores), ni `extra` (los argumentos de un trabajo en cola). De la petición quedan el método y la ruta, sin la
  consulta.

Es la primera capa. La segunda es el depurador de datos del propio proyecto de Sentry (deploy/monitoring/README.md).
"""

from __future__ import annotations

import copy
import re

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
SECRET = re.compile(
	r"(?i)\b(api[_-]?key|api[_-]?secret|key|secret|token|password|passwd|pwd|sid|signature|authorization)\b"
	r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s&,;]+)")
# seis cifras o más, con espacios, puntos, guiones o barras entre ellas: +598 99 123 456, 1.234.567-8, 4509 9535…,
# 220645600013, 1984-03-07
NUMBER = re.compile(r"\+?\d(?:[ ./-]?\d){5,}")
BOLD = re.compile(r"(?is)<(strong|b)>.*?</\1>")
QUOTED = re.compile(r"'([^'\n]{1,300})'|\"([^\"\n]{1,300})\"")
# lo que puede quedar entre comillas: una ruta de fichero .py, un nombre con punto o guion bajo, o una marca nuestra
SAFE_QUOTED = re.compile(r"[\w./\\-]+\.py|[A-Za-z_]\w*(?:[._]\w+)+|\[(?:correo|número|texto|secreto)\]")

TOP_KEYS = ("event_id", "timestamp", "level", "platform", "logger", "server_name", "release", "environment", "dist",
            "sdk", "fingerprint", "type")
CONTEXTS = ("runtime", "os", "trace")
TAGS = ("error_log", "titulo")
FRAME_KEYS = ("filename", "abs_path", "function", "module", "lineno", "colno", "in_app", "context_line",
              "pre_context", "post_context", "platform")


def _quoted(match: re.Match) -> str:
	quote = "'" if match.group(1) is not None else '"'
	inner = match.group(1) if match.group(1) is not None else match.group(2)
	return match.group(0) if SAFE_QUOTED.fullmatch(inner) else f"{quote}[texto]{quote}"


def text(value) -> str:
	"""El texto sin datos personales (ver el docstring del módulo). None da ""."""
	if value is None:
		return ""
	out = str(value)
	out = EMAIL.sub("[correo]", out)
	out = SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}[secreto]", out)
	out = NUMBER.sub("[número]", out)
	out = BOLD.sub(lambda m: f"<{m.group(1)}>[texto]</{m.group(1)}>", out)
	return QUOTED.sub(_quoted, out)


EXCEPTION_LINE = re.compile(r"([A-Za-z_][\w.]*)(?::.*)?")
TRACEBACK_KEEP = ("Traceback", "During handling of the above exception", "The above exception was the direct cause")


def traceback(value) -> str:
	"""La estructura de una traza en texto, sin nada de lo que dice: las cabeceras, cada `File …, line N, in f` con su
	línea de código, y de cada excepción solo su tipo (su mensaje es texto libre y puede llevar un nombre)."""
	out, after_file = [], False
	for line in str(value or "").splitlines():
		if line.startswith("  File "):
			out.append(line)
			after_file = True
			continue
		if after_file and line.startswith("    ") and line.strip():
			out.append(line)  # la línea de código de ese marco (las variables volcadas, más adentro, no)
		elif line.startswith(TRACEBACK_KEEP):
			out.append(line)
		elif line and not line[0].isspace() and (m := EXCEPTION_LINE.fullmatch(line)):
			out.append(m.group(1))
		after_file = False
	return text("\n".join(out))


def _exception_messages(trace: str) -> dict:
	"""{mensaje: tipo} de las líneas de excepción de una traza."""
	found = {}
	for line in str(trace or "").splitlines():
		if line and not line[0].isspace() and ":" in line:
			kind, _, msg = line.partition(":")
			if EXCEPTION_LINE.fullmatch(kind) and msg.strip():
				found[msg.strip()] = kind
	return found


def title(value, trace: str = "") -> str:
	"""El título de un Error Log: si es el mensaje de la excepción (Frappe lo pone así en los errores de una
	petición), su tipo; si no, filtrado."""
	raw = str(value or "").strip()
	return _exception_messages(trace).get(raw) or text(raw)


def message(value) -> str:
	"""«título\n\ntraza» (lo que manda Frappe y lo que arma sentry.py): el título por `title`, la traza por
	`traceback`."""
	first, _, rest = str(value or "").partition("\n")
	head = title(first, rest)
	body = traceback(rest)
	return f"{head}\n\n{body}" if body else head


def _path(url):
	"""Una URL o una ruta sin su consulta ni su fragmento."""
	return re.split(r"[?#]", url, maxsplit=1)[0] if isinstance(url, str) else None


def _frame(frame: dict) -> dict:
	return {k: copy.deepcopy(frame[k]) for k in FRAME_KEYS if k in frame}


def _stacktrace(stacktrace) -> dict:
	frames = (stacktrace or {}).get("frames") or []
	return {"frames": [_frame(f) for f in frames if isinstance(f, dict)]}


def _exception(value: dict) -> dict:
	# sin `value`: el mensaje de una excepción es texto libre (en Kamra, el Error Log que nombra la etiqueta
	# error_log lo guarda entero)
	out = {k: value[k] for k in ("type", "module") if k in value}
	mechanism = value.get("mechanism")
	if isinstance(mechanism, dict):
		out["mechanism"] = {k: mechanism[k] for k in ("type", "handled") if k in mechanism}
	if value.get("stacktrace"):
		out["stacktrace"] = _stacktrace(value["stacktrace"])
	return out


def event(ev: dict, hint=None) -> dict:
	"""before_send de sentry-sdk: un evento nuevo con solo lo de la lista, filtrado (ver el docstring del módulo)."""
	out = {k: copy.deepcopy(ev[k]) for k in TOP_KEYS if k in ev}
	exception = ev.get("exception")
	if isinstance(exception, dict):
		out["exception"] = {"values": [_exception(v) for v in exception.get("values") or [] if isinstance(v, dict)]}
	if "message" in ev:
		out["message"] = message(ev["message"])
	logentry = ev.get("logentry")
	if isinstance(logentry, dict):
		out["logentry"] = {k: message(logentry[k]) for k in ("message", "formatted") if k in logentry}
	contexts = ev.get("contexts")
	if isinstance(contexts, dict):
		out["contexts"] = {k: copy.deepcopy(contexts[k]) for k in CONTEXTS if k in contexts}
	tags = ev.get("tags")
	if isinstance(tags, dict):
		out["tags"] = {k: text(v)[:200] for k, v in tags.items() if k in TAGS}
	request = ev.get("request")
	if isinstance(request, dict):
		out["request"] = {k: v for k, v in (("method", request.get("method")), ("url", _path(request.get("url"))))
		                  if v}
	if "transaction" in ev:
		out["transaction"] = _path(ev["transaction"]) or ""
	return out
