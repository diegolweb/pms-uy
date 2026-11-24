"""El formulario de contacto de la web del hotel (HR-314): la consulta llega por correo a recepción.

La página /contacto (y /pt/contato, /en/contact) llama sin sesión, por POST, a `send`, una de las rutas públicas de la
web (web/src/booking/rutas-kamra.json): el borde la abre con el CORS de la reserva (HR-248 en dev, HR-249 en producción)
y no hace falta ningún manejador en el hosting de la web, que no está decidido (D-HR-52).

- Valida todo en el servidor (kamra_uy.pure.contact) y devuelve códigos, no textos: la web los traduce.
- Contra el spam, sin terceros ni cookies: el campo trampa (responde «ok» y no manda nada), 3 s desde la carga de la
  página y el contador de Frappe por IP (`rate_limit`, 10 por hora, el mismo de `book`).
- Encola UN correo, en español (es para recepción), al destinatario del ajuste de la Property de la web (`FIELD`; por
  defecto `DEFAULT_RECIPIENT`, la dirección que confirmó el fundador el 23-11), con el Reply-To del huésped: recepción
  contesta desde su correo. Sale por la cuenta saliente de Kamra (la de Resend en producción, D-HR-41), como el de
  confirmación.
- Lo que se guarda: el correo vive en la cola de Frappe (Email Queue) hasta que sale. Al salir, Frappe borra su cuerpo
  (`redact_message_after_send`) y `purge` borra la fila entera (con el asunto, el Reply-To y su destinatario) en la
  pasada de cada hora. Uno que no llega a salir (sin cuenta, o con errores del envío) se borra a los `UNSENT_DAYS` días.
  Nada más: ni un DocType, ni una Communication, ni datos del huésped en el Error Log.
"""

from __future__ import annotations

from html import escape

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils import add_days, now_datetime

from kamra.public_api import default_property
from kamra_uy.pure.contact import check

MODULE = "Kamra UY"
FIELD = "uy_contact_email"
DEFAULT_RECIPIENT = "recepcion@hotelrooseveltpunta.com"
LIMIT = 10  # por hora e IP, como kamra.public_api.book
MESSAGE_ID_PREFIX = "kamra-uy-contacto."
UNSENT_DAYS = 7
LOG_TITLE = "kamra_uy: consulta de la web sin enviar"
_SAVEPOINT = "kamra_uy_contact_mail"
IDIOMAS = {"es": "Español", "pt": "Português", "en": "English"}

FIELDS = {
	"Property": [
		{"fieldname": FIELD, "label": "Correo de las consultas de la web", "fieldtype": "Data", "options": "Email",
		 "default": DEFAULT_RECIPIENT, "insert_after": "email", "module": MODULE,
		 "description": "Adónde llegan los mensajes del formulario de contacto de la web (HR-314). Vacío, a "
		                f"{DEFAULT_RECIPIENT}."},
	],
}


def ensure_fields() -> None:
	"""Crea o pone al día el ajuste en la Property (after_install, after_migrate). Idempotente."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(FIELDS, update=True)


def recipient(property: str) -> str:
	"""El destinatario de las consultas de la web de `property`: su ajuste o, vacío, el de por defecto."""
	valor = frappe.db.get_value("Property", property, FIELD) if frappe.db.has_column("Property", FIELD) else None
	return (valor or "").strip() or DEFAULT_RECIPIENT


# xss_safe: lo del huésped llega tal cual y compose() lo escapa al ponerlo en el correo. Sin él, Frappe pasa todo lo de un
# invitado por sanitize_html, que borra un <script>…</script> entero (un mensaje así llegaba vacío: «requerido») y deja
# pasar <b>; lo que escribió el huésped no llegaba a recepción como lo escribió.
@frappe.whitelist(allow_guest=True, methods=["POST"], xss_safe=True)
@rate_limit(limit=LIMIT, seconds=3600)
def send(nombre: str = "", apellido: str = "", email: str = "", telefono: str = "", mensaje: str = "",
         sitio: str = "", espera: str = "") -> dict:
	"""{"ok": true} | {"ok": false, "code": "invalido", "errors": {campo: código}} | {"ok": false, "code": "rapido"} |
	{"ok": false, "code": "sin-envio"}. El campo trampa lleno contesta {"ok": true} y no manda nada."""
	form = frappe.local.form_dict or {}
	r = check({"nombre": nombre, "apellido": apellido, "email": email, "telefono": telefono, "mensaje": mensaje,
	           "sitio": sitio, "espera": espera, "_lang": form.get("_lang") or getattr(frappe.local, "lang", None)})
	if r.spam == "trampa":
		return {"ok": True}
	if r.spam:
		return {"ok": False, "code": r.spam}
	if not r.ok:
		return {"ok": False, "code": "invalido", "errors": r.errors}
	mail = compose(r.data, recipient(default_property()))
	frappe.db.savepoint(_SAVEPOINT)
	try:
		frappe.sendmail(recipients=[mail["to"]], subject=mail["subject"], message=mail["message"],
		                reply_to=mail["reply_to"], message_id=f"{MESSAGE_ID_PREFIX}{frappe.generate_hash(length=20)}"
		                f"@{frappe.local.site}", delayed=True, add_unsubscribe_link=0,
		                redact_message_after_send=True)
	except Exception as e:
		frappe.db.rollback(save_point=_SAVEPOINT)
		# sin traza: ni el huésped ni las variables locales (HR-21)
		frappe.log_error(title=LOG_TITLE, message=type(e).__name__)
		return {"ok": False, "code": "sin-envio"}
	frappe.db.release_savepoint(_SAVEPOINT)
	return {"ok": True}


_TD = "padding:8px 0;border-bottom:1px solid #E4E1EC;vertical-align:top;"


def _fila(rotulo: str, valor_html: str) -> str:
	return (f'<tr><td style="{_TD}padding-right:16px;">{escape(rotulo)}</td>'
	        f'<td style="{_TD}"><strong>{valor_html}</strong></td></tr>')


def compose(data: dict, to: str) -> dict:
	"""{to, subject, message, reply_to} del correo a recepción. Todo lo del huésped va escapado; el asunto, en una línea
	(kamra_uy.pure.contact ya quitó los saltos y los caracteres de control)."""
	quien = f"{data['nombre']} {data['apellido']}"
	filas = [("Nombre", escape(data["nombre"])), ("Apellido", escape(data["apellido"])),
	         ("Email", f'<a href="mailto:{escape(data["email"])}">{escape(data["email"])}</a>')]
	if data["telefono"]:
		tel = "".join(c for c in data["telefono"] if c.isdigit() or c == "+")
		filas.append(("Teléfono", f'<a href="tel:{escape(tel)}">{escape(data["telefono"])}</a>'))
	filas.append(("Idioma de la página", escape(IDIOMAS[data["idioma"]])))
	cuerpo = escape(data["mensaje"]).replace("\n", "<br>")
	html = (
		'<h1 style="margin:0 0 16px 0;font-size:24px;line-height:32px;">Consulta de la web</h1>'
		f"<p>{escape(quien)} escribió desde el formulario de contacto de la web. Responde a este correo y tu respuesta "
		f"le llega a {escape(data['email'])}.</p>"
		'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
		'style="border-collapse:collapse;margin:16px 0 24px 0;">' + "".join(_fila(*f) for f in filas) + "</table>"
		'<h2 style="margin:24px 0 8px 0;font-size:18px;line-height:24px;">Mensaje</h2>'
		f'<p style="margin:0;">{cuerpo}</p>'
	)
	return {"to": to, "subject": f"Consulta de la web: {quien}", "message": html, "reply_to": data["email"]}


def purge() -> None:
	"""Cada hora (scheduler_events): borra de la cola de Frappe las consultas de la web que ya salieron y las que no
	salieron en `UNSENT_DAYS` días, con sus destinatarios. Las demás filas de la cola no se tocan."""
	queue = frappe.qb.DocType("Email Queue")
	ours = queue.message_id.like(f"{MESSAGE_ID_PREFIX}%")
	names = (
		frappe.qb.from_(queue).select(queue.name)
		.where(ours & ((queue.status == "Sent") | (queue.creation < add_days(now_datetime(), -UNSENT_DAYS))))
	).run(pluck=True)
	if not names:
		return
	frappe.db.delete("Email Queue Recipient", {"parent": ["in", names]})
	frappe.db.delete("Email Queue", {"name": ["in", names]})
