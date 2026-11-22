"""Sentry sin datos personales (HR-21).

Qué sale: cada Error Log de Kamra (los 500 de las peticiones, los trabajos en cola y programados que fallan y los
`frappe.log_error` explícitos: todos acaban ahí) como un evento de Sentry: el tipo del error y su traza (ficheros,
funciones, líneas y código), sin su mensaje ni sus variables locales, con el título filtrado y la etiqueta
`error_log` = el nombre del Error Log, que guarda el detalle completo DENTRO de Kamra. Todo pasa por
kamra_uy/health/scrub.py.

Dónde se configura: el DSN va en site_config.json (`hr_sentry_dsn`, lo pone deploy/monitoring/instala.sh desde el
fichero del fundador) y, opcionales, `hr_sentry_environment` y `hr_sentry_release`. No hace falta tocar el compose de
producción. Si alguien define además FRAPPE_SENTRY_DSN en el entorno, Frappe crea su propio cliente y manda él los
errores (frappe/utils/error.py); entonces este módulo no los manda otra vez, pero sí le pone el filtro a ese cliente
(`setup`, desde before_request y before_job), le quita las migas y las variables locales, y quita el DSN del arranque
del escritorio (`extend_bootinfo`): así ningún error del navegador sale sin filtro.

Ganchos (kamra_uy/hooks.py): doc_events «Error Log».after_insert → on_error_log; before_request y before_job → setup;
extend_bootinfo → extend_bootinfo. Nada de esto puede romper una petición ni el registro de un error: cualquier fallo
de Sentry se queda en el log de Frappe.
"""

from __future__ import annotations

import os
import sys

import frappe

from kamra_uy.health import scrub

ENV_DSN = "FRAPPE_SENTRY_DSN"
CONF_DSN = "hr_sentry_dsn"
MESSAGE_MAX = 6000


def dsn() -> str:
	return (os.environ.get(ENV_DSN) or frappe.conf.get(CONF_DSN) or "").strip()


def frappe_sends() -> bool:
	"""Con FRAPPE_SENTRY_DSN, Frappe ya manda cada Error Log (y este módulo solo lo filtra)."""
	return bool(os.environ.get(ENV_DSN))


def _transport():
	"""El transporte de sentry-sdk (None: el suyo, por HTTPS al DSN). Los tests ponen una lista."""
	return None


def _no_breadcrumb(crumb, hint):
	return None


def _chain(before):
	def before_send(event, hint):
		if before is not None:
			event = before(event, hint)
			if event is None:
				return None
		return scrub.event(event, hint)

	before_send.hr21_scrub = True
	return before_send


def _harden(client) -> None:
	options = client.options
	if not getattr(options.get("before_send"), "hr21_scrub", False):
		options["before_send"] = _chain(options.get("before_send"))
	options["before_breadcrumb"] = _no_breadcrumb
	options["send_default_pii"] = False
	# además de no sacar datos: con Python 3.14, sentry-sdk 1.45 no sabe copiar las variables locales
	# (TypeError: cannot pickle 'FrameLocalsProxy') y el cliente de Frappe, que las pide, no manda ningún error
	options["include_local_variables"] = False
	options["max_request_body_size"] = "never"


def _init(url: str) -> None:
	import sentry_sdk
	from sentry_sdk.integrations.dedupe import DedupeIntegration

	sentry_sdk.init(
		dsn=url, transport=_transport(), default_integrations=False, auto_enabling_integrations=False,
		integrations=[DedupeIntegration()], send_default_pii=False, include_local_variables=False,
		# sin traces_sample_rate: con 0, sentry-sdk 1.45 descarta también los errores (visto en la imagen, 21-11)
		max_request_body_size="never", before_send=scrub.event, before_breadcrumb=_no_breadcrumb,
		environment=str(frappe.conf.get("hr_sentry_environment") or "produccion"),
		release=str(frappe.conf.get("hr_sentry_release") or "") or None)


def setup() -> bool:
	"""El cliente de Sentry de este proceso, filtrado; lo crea si hay DSN y no lo hay. True si queda uno."""
	try:
		import sentry_sdk

		client = sentry_sdk.Hub.current.client
		if client is None:
			url = dsn()
			if not url:
				return False
			_init(url)
			client = sentry_sdk.Hub.current.client
		if client is None:
			return False
		_harden(client)
		return True
	except Exception:
		frappe.logger("kamra_uy.health").exception("sentry setup")
		return False


def on_error_log(doc, method=None) -> None:
	"""Error Log.after_insert: el error, filtrado, a Sentry. Nunca lanza: un Sentry roto no tapa el Error Log."""
	try:
		if frappe_sends() or not setup():
			return
		import sentry_sdk

		title = scrub.title(doc.method, doc.error or "")[:200]
		with sentry_sdk.push_scope() as scope:
			scope.set_tag("error_log", doc.name)
			scope.set_tag("titulo", title)
			if doc.get("fingerprint"):
				scope.fingerprint = [doc.fingerprint]
			if sys.exc_info()[1] is not None:
				sentry_sdk.capture_exception()
			else:  # inserción diferida (los 500 de una petición): la traza en texto, solo su estructura
				sentry_sdk.capture_message(f"{title}\n\n{scrub.traceback(doc.error)[:MESSAGE_MAX]}", level="error")
		sentry_sdk.flush(timeout=2)  # un trabajo en cola sale del proceso al acabar: que no se quede en la cola
	except Exception:
		frappe.logger("kamra_uy.health").exception("sentry capture")


def extend_bootinfo(bootinfo) -> None:
	"""El escritorio no recibe el DSN: los errores del navegador no pasan por este filtro."""
	bootinfo.pop("sentry_dsn", None)
