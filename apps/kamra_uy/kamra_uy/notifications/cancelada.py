"""El aviso de cancelación al huésped (HR-374; el fundador, 26-11: «Te doy el ok a esos otros 2 emails»).

Cuando una reserva confirmada pasa a Cancelled (kamra.api.cancel_reservation, desde recepción o desde la web), kamra_uy
encola UN correo al huésped con la misma plantilla que la confirmación (kamra_uy.branding.correo.envolver), en el idioma
de la reserva, desde el correo de la Property y con las respuestas al mismo. Lleva la referencia, el número de
cancelación, las fechas, la habitación y, si Kamra cobró algo al cancelar, cuánto. Mismas reglas que la confirmación:
solo si el huésped tiene correo, solo las de la web y las de recepción, nunca una de plataforma (les escribe la
plataforma), y una sola vez (`uy_cancellation_mail_on`). Una reserva que nunca se confirmó (retenida que caduca o se
suelta) no lo lleva: no llegó a ser suya. Sus textos van tras texts.BORRADORES_OK: apagado, no sale. Y nunca
frena la cancelación: si no puede encolarse, se deshace solo el correo y su porqué va al Error Log de la reserva.
"""

from __future__ import annotations

from html import escape

import frappe
from frappe.utils import flt, now_datetime

from kamra_uy.branding import correo, marca
from kamra_uy.notifications import confirmation, formato, texts
from kamra_uy.notifications.texts import TEXTOS

MODULE = confirmation.MODULE
SENT_FIELD = "uy_cancellation_mail_on"
LOG_TITLE = "kamra_uy: aviso de cancelación sin enviar"
_SAVEPOINT = "kamra_uy_cancellation_mail"
SOURCES = (confirmation.SOURCE, *confirmation.RECEPCION)

FIELDS = {
	"Reservation": [
		{"fieldname": SENT_FIELD, "label": "Aviso de cancelación encolado", "fieldtype": "Datetime", "read_only": 1,
		 "no_copy": 1, "print_hide": 1, "insert_after": confirmation.SENT_FIELD, "module": MODULE,
		 "description": "Cuándo se encoló el aviso de cancelación al huésped: sale una sola vez."},
	],
}


def ensure_fields() -> None:
	"""El campo de la Reservation (after_install, after_migrate). Idempotente."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(FIELDS, update=True)


def _cancelled_now(doc) -> bool:
	if not texts.BORRADORES_OK or doc.get("source") not in SOURCES or confirmation.de_plataforma(doc):
		return False
	if doc.get("status") != "Cancelled" or doc.get(SENT_FIELD) or confirmation._quiet():
		return False
	before = doc.get_doc_before_save()
	return bool(before) and before.get("status") == "Confirmed"


def on_reservation_update(doc, method=None) -> None:
	"""on_update de Reservation: la reserva confirmada que se cancela se lleva su aviso, una vez."""
	if _cancelled_now(doc):
		send(doc)


def send(res) -> bool:
	"""Encola el aviso de `res` y lo marca. False si no hay a quién o no pudo encolarse (Error Log)."""
	frappe.db.savepoint(_SAVEPOINT)
	try:
		mail = None if frappe.db.get_value("Reservation", res.name, SENT_FIELD, for_update=True) else compose(res)
		if mail:
			frappe.sendmail(recipients=[mail["to"]], subject=mail["subject"], message=mail["message"],
			                sender=mail["sender"], reply_to=mail["reply_to"], reference_doctype="Reservation",
			                reference_name=res.name)
			sent = now_datetime()
			frappe.db.set_value("Reservation", res.name, SENT_FIELD, sent, update_modified=False)
	except Exception as e:
		frappe.db.rollback(save_point=_SAVEPOINT)
		frappe.log_error(title=LOG_TITLE, message=type(e).__name__, reference_doctype="Reservation",
		                 reference_name=res.name)  # sin traza: ni el destinatario ni las variables locales
		return False
	frappe.db.release_savepoint(_SAVEPOINT)
	if not mail:
		return False
	res.set(SENT_FIELD, sent)
	return True


def compose(res) -> dict | None:
	to = confirmation.guest_email(res)
	if not to:
		return None
	prop = frappe.db.get_value("Property", res.property, confirmation.PROPERTY_FIELDS, as_dict=True)
	idioma = marca.idioma(res.get(confirmation.LANGUAGE_FIELD) or prop.booking_language)
	datos = {
		"hotel": prop.property_name or res.property,
		"nombre": (res.get("guest_name") or "").strip(),
		"referencia": res.name,
		"numero": (res.get("cancellation_number") or "").strip(),
		"llegada": res.check_in_date,
		"salida": res.check_out_date,
		"habitacion": confirmation.room_name(res, idioma),
		"cargo": flt(res.get("cancellation_fee")),
		"moneda": (prop.currency or "").upper(),
		"telefono": (prop.phone or "").strip(),
		"correo": (prop.email or "").strip(),
	}
	subject, html = render(datos, idioma)
	return {"to": to, "subject": subject, "message": correo.envolver(html, idioma),
	        "sender": confirmation.sender(prop), "reply_to": datos["correo"] or None, "idioma": idioma}


def render(datos: dict, idioma: str) -> tuple[str, str]:
	"""(asunto, cuerpo HTML) en `idioma`, con las piezas de la confirmación. Todo lo de la reserva va escapado."""
	t, b = TEXTOS[idioma], texts.BORRADORES[idioma]
	filas = [(t["referencia"], datos["referencia"])]
	if datos["numero"]:
		filas.append((b["numero_cancelacion"], datos["numero"]))
	filas += [
		(t["llegada"], formato.fecha_corta(datos["llegada"], idioma)),
		(t["salida"], formato.fecha_corta(datos["salida"], idioma)),
		(t["habitacion"], datos["habitacion"]),
	]
	if datos["cargo"] > 0.005:
		filas.append((b["cargo_cancelacion"], formato.importe(datos["cargo"], datos["moneda"], idioma)))
	partes = [
		f'<h1 style="margin:0 0 16px 0;font-size:24px;line-height:32px;">{escape(b["cancelada"])}</h1>',
		f"<p>{escape(t['saludo'].format(nombre=datos['nombre']))}</p>" if datos["nombre"] else "",
		f"<p>{escape(b['intro_cancelada'])}</p>",
		'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
		'style="border-collapse:collapse;margin:16px 0 24px 0;">'
		+ "".join(confirmation._fila(*f) for f in filas) + "</table>",
	]
	contacto = [escape(datos["telefono"])] if datos["telefono"] else []
	if datos["correo"]:
		contacto.append(correo.enlace(datos["correo"], f"mailto:{datos['correo']}"))
	if contacto:
		partes.append(f'<h2 style="margin:24px 0 4px 0;font-size:18px;line-height:24px;">{escape(t["contacto"])}</h2>'
		              f'<p style="margin:0;">{" · ".join(contacto)}</p>')
	partes.append(f'<p style="margin-top:24px;">{escape(b["ayuda_cancelada"])}</p>')
	partes.append(f"<p>{escape(b['despedida_cancelada'].format(hotel=datos['hotel']))}</p>")
	return b["asunto_cancelada"].format(hotel=datos["hotel"]), "".join(p for p in partes if p)
