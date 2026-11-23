"""El correo de confirmación de la reserva en línea (HR-227).

La página de confirmada de la web promete «Te enviamos la confirmación por email» y Kamra no lo manda: `book`
(kamra.public_api) no avisa a nadie. Lo manda kamra_uy, con dos ganchos de Reservation (hooks.py):

- `remember_language` (before_insert): la reserva web guarda el idioma de la página con que se hizo, el `_lang` que la
  web manda en cada llamada a Kamra (es, pt o en; con otro, nada). Sin `_lang`, el de la petición (Accept-Language).
  Kamra no lo guarda en ningún sitio, y el pago llega después por el aviso de core, sin la petición del huésped.
- `on_reservation_update` (on_update): cuando una reserva Website pasa de retenida (Pending Payment o Held) a Confirmed
  con dinero cobrado (`advance_paid` > 0) —el aviso `payment.approved` de core, kamra_uy.payments.webhook, que asienta
  el pago y confirma con kamra.api._confirm_status—, encola UN correo al correo del huésped, en el idioma guardado (sin
  él, el de la propiedad, `booking_language`; sin ese, español). Lleva la referencia, las fechas, las noches, la
  habitación, los huéspedes, el total pagado, el horario de check-in y check-out, la cancelación (HR-291:
  kamra_uy.notifications.cancelacion, con los campos de la Property), la dirección y el contacto de la propiedad y, si alguna noche cae en la alta temporada (kamra_uy.pure.turismo, D-HR-40), el aviso del IVA a residentes.
  La identidad (logo, letra, colores, pie) se la pone HR-32 (kamra_uy.branding.correo.envolver).

Un solo correo por reserva: lo marca `uy_confirmation_mail_on` al encolarlo; otro aviso del mismo pago, otro pago, otro
guardado o una vuelta a retenida y otra confirmación no lo repiten. Nunca lleva un dato del pago (ni el modo, ni la
referencia, ni la tarjeta): solo importes de la reserva. Y nunca frena la confirmación: si no se puede montar o
encolar (sin cuenta de correo saliente, sendmail LANZA), se deshace solo el correo y su porqué (el tipo del error, sin
datos personales) va al Error Log de la reserva (HR-224).

Los campos `uy_booking_language` y `uy_confirmation_mail_on` los crea `ensure_fields` (after_install y after_migrate).
"""

from __future__ import annotations

import re
from html import escape

import frappe
from frappe.utils import cint, date_diff, flt, now_datetime

from kamra_uy.branding import correo, marca
from kamra_uy.notifications import cancelacion, formato
from kamra_uy.notifications.texts import TEXTOS
from kamra_uy.pure.turismo import aviso_iva_residentes

MODULE = "Kamra UY"
LANGUAGE_FIELD = "uy_booking_language"
SENT_FIELD = "uy_confirmation_mail_on"
SOURCE = "Website"
HELD = ("Pending Payment", "Held")
LOG_TITLE = "kamra_uy: correo de confirmación de la reserva en línea sin enviar"
_SAVEPOINT = "kamra_uy_confirmation_mail"

FIELDS = {
	"Reservation": [
		{"fieldname": LANGUAGE_FIELD, "label": "Idioma de la reserva en línea", "fieldtype": "Data", "read_only": 1,
		 "no_copy": 1, "print_hide": 1, "insert_after": "source", "module": MODULE,
		 "description": "El idioma de la página con que el huésped reservó en la web (es, pt o en): el del correo de "
		                "confirmación."},
		{"fieldname": SENT_FIELD, "label": "Correo de confirmación encolado", "fieldtype": "Datetime", "read_only": 1,
		 "no_copy": 1, "print_hide": 1, "insert_after": LANGUAGE_FIELD, "module": MODULE,
		 "description": "Cuándo se encoló el correo de confirmación de la reserva en línea: sale una sola vez."},
	],
}


def ensure_fields() -> None:
	"""Crea o pone al día los campos de la Reservation (after_install, after_migrate). Idempotente."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(FIELDS, update=True)


def _codigo(valor) -> str | None:
	"""«pt-BR», «PT_br» o «pt» → pt; uno que la web no tiene, o ninguno → None."""
	base = re.split(r"[-_]", str(valor or "").strip().lower(), maxsplit=1)[0]
	return base if base in marca.IDIOMAS else None


def _in_request() -> bool:
	return bool(getattr(frappe.local, "request", None))


def _page_language() -> str | None:
	"""El idioma de la página que llama. Dentro de `book` el `_lang` ya no está en form_dict: kamra.authz.act_as llama a
	frappe.set_user, que lo vacía. Pero Frappe lo dejó en frappe.local.lang al empezar la petición
	(frappe.auth.HTTPRequest.set_lang → frappe.translate.get_language: `_lang`, cookie, Accept-Language), y set_user no
	lo toca. Fuera de una petición (consola, trabajos), frappe.local.lang es el del sistema: no dice nada del huésped."""
	codigo = _codigo((frappe.local.form_dict or {}).get("_lang"))
	if not codigo and _in_request():
		codigo = _codigo(getattr(frappe.local, "lang", None))
	return codigo


def remember_language(doc, method=None) -> None:
	"""before_insert de Reservation: el idioma de la página de la reserva web."""
	if doc.get("source") != SOURCE or doc.get(LANGUAGE_FIELD):
		return
	codigo = _page_language()
	if codigo:
		doc.set(LANGUAGE_FIELD, codigo)


def _confirmed_by_payment(doc) -> bool:
	if doc.get("source") != SOURCE or doc.get("status") != "Confirmed" or flt(doc.get("advance_paid")) <= 0:
		return False
	if doc.get(SENT_FIELD) or any(frappe.flags.get(f) for f in ("in_import", "in_install", "in_migrate", "in_patch")):
		return False
	before = doc.get_doc_before_save()
	return bool(before) and before.get("status") in HELD


def on_reservation_update(doc, method=None) -> None:
	"""on_update de Reservation: la reserva web que confirma un pago se lleva su correo, una vez."""
	if _confirmed_by_payment(doc):
		send(doc)


def send(res) -> bool:
	"""Encola el correo de confirmación de `res` y lo marca. False si no hay a quién o no pudo encolarse (Error Log)."""
	frappe.db.savepoint(_SAVEPOINT)
	try:
		if frappe.db.get_value("Reservation", res.name, SENT_FIELD, for_update=True):
			mail = None
		else:
			mail = compose(res)
		if mail:
			frappe.sendmail(recipients=[mail["to"]], subject=mail["subject"], message=mail["message"],
			                reply_to=mail["reply_to"], reference_doctype="Reservation", reference_name=res.name)
			sent = now_datetime()
			frappe.db.set_value("Reservation", res.name, SENT_FIELD, sent, update_modified=False)
	except Exception as e:
		frappe.db.rollback(save_point=_SAVEPOINT)
		# sin traza: ni el destinatario ni las variables locales (HR-21)
		frappe.log_error(title=LOG_TITLE, message=type(e).__name__, reference_doctype="Reservation",
		                 reference_name=res.name)
		return False
	frappe.db.release_savepoint(_SAVEPOINT)
	if not mail:
		return False
	res.set(SENT_FIELD, sent)  # que otro save de esta misma copia no lo borre
	return True


def compose(res) -> dict | None:
	"""{to, subject, message, reply_to, idioma} del correo de `res`, o None si el huésped no tiene correo."""
	to = (frappe.db.get_value("Guest", res.guest, "email") or "").strip() if res.get("guest") else ""
	if not to:
		return None
	prop = frappe.db.get_value(
		"Property", res.property,
		["property_name", "address_line", "city", "phone", "email", "currency", "checkin_time", "checkout_time",
		 "booking_language", "free_cancel_days", "cancellation_fee", "no_show_charge"], as_dict=True)
	idioma = marca.idioma(res.get(LANGUAGE_FIELD) or prop.booking_language)
	datos = {
		"hotel": prop.property_name or res.property,
		"nombre": (res.get("guest_name") or "").strip(),
		"referencia": res.name,
		"llegada": res.check_in_date,
		"salida": res.check_out_date,
		"noches": cint(res.get("nights")) or date_diff(res.check_out_date, res.check_in_date),
		"habitacion": (frappe.db.get_value("Room Type", res.room_type, "room_type_name") if res.get("room_type")
		               else None) or res.get("room_type") or "",
		"huespedes": cint(res.get("adults")) + cint(res.get("children")),
		"total": flt(res.get("amount_after_tax")),
		"pagado": flt(res.get("advance_paid")),
		"moneda": (prop.currency or "").upper(),
		"entrada": formato.hora(prop.checkin_time),
		"salida_hora": formato.hora(prop.checkout_time),
		"direccion": ", ".join(x.strip() for x in (prop.address_line, prop.city) if x and x.strip()),
		"telefono": (prop.phone or "").strip(),
		"correo": (prop.email or "").strip(),
		"cancelacion": _cancelacion(res, prop),
	}
	subject, html = render(datos, idioma)
	return {"to": to, "subject": subject, "message": correo.envolver(html, idioma), "reply_to": datos["correo"] or None,
	        "idioma": idioma}


def _cancelacion(res, prop) -> dict:
	"""La política de cancelación de `res` con los campos de su Property, como la cobra Kamra
	(kamra.api._cancellation_terms): los tramos de la Property mandan sobre `free_cancel_days`, y una noche en una
	temporada o un paquete `non_refundable` (kamra.pricing.stay_season_rules, la misma llamada) manda sobre los dos."""
	from kamra.pricing import stay_season_rules

	tramos = frappe.db.exists("Cancellation Tier", {"parenttype": "Property", "parentfield": "cancellation_tiers",
	                                                "parent": res.property})
	reglas = stay_season_rules(res.property, res.check_in_date, res.check_out_date, res.room_type)
	return {"dias": prop.free_cancel_days, "cargo": prop.cancellation_fee, "no_show": prop.no_show_charge,
	        "tramos": bool(tramos), "no_reembolsable": bool(reglas["non_refundable"])}


# --- el cuerpo ---------------------------------------------------------------------------------------------------

_TD = "padding:8px 0;border-bottom:1px solid #E4E1EC;vertical-align:top;"


def _fila(rotulo: str, valor: str) -> str:
	return (f'<tr><td style="{_TD}padding-right:16px;">{escape(rotulo)}</td>'
	        f'<td style="{_TD}text-align:right;"><strong>{escape(valor)}</strong></td></tr>')


def render(datos: dict, idioma: str) -> tuple[str, str]:
	"""(asunto, cuerpo HTML) en `idioma` (es, pt o en). Todo lo que viene de la reserva va escapado."""
	t = TEXTOS[idioma]
	total = formato.importe(datos["total"], datos["moneda"], idioma)
	filas = [
		(t["referencia"], datos["referencia"]),
		(t["llegada"], formato.fecha_corta(datos["llegada"], idioma)),
		(t["salida"], formato.fecha_corta(datos["salida"], idioma)),
		(t["noches"], str(datos["noches"])),
		(t["habitacion"], datos["habitacion"]),
		(t["huespedes"], str(datos["huespedes"])),
	]
	# como la página de confirmada: «Total US$ 390 · Pagado», o el total y lo pagado si no se pagó todo
	if datos["pagado"] >= datos["total"] - 0.005:
		filas.append((t["total"], f"{total} · {t['pagado']}"))
	else:
		filas += [(t["total"], total), (t["pagado"], formato.importe(datos["pagado"], datos["moneda"], idioma))]

	partes = [
		f'<h1 style="margin:0 0 16px 0;font-size:24px;line-height:32px;">{escape(t["confirmada"])}</h1>',
		f"<p>{escape(t['saludo'].format(nombre=datos['nombre']))}</p>" if datos["nombre"] else "",
		f"<p>{escape(t['intro'])}</p>",
		'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
		'style="border-collapse:collapse;margin:16px 0 24px 0;">' + "".join(_fila(*f) for f in filas) + "</table>",
	]
	if aviso_iva_residentes(datos["llegada"], datos["salida"]):
		partes.append(f'<p data-aviso="iva">{escape(t["iva"])}</p>')
	if datos["entrada"] and datos["salida_hora"]:
		partes.append(f"<p>{escape(t['horario'].format(entrada=datos['entrada'], salida=datos['salida_hora']))}</p>")
	if datos.get("cancelacion"):
		c = datos["cancelacion"]
		titulo, textos = cancelacion.parrafos(idioma, c["dias"], c["cargo"], c["no_show"], tramos=c["tramos"],
		                                      no_reembolsable=c["no_reembolsable"])
		partes.append(f'<h2 style="margin:24px 0 4px 0;font-size:18px;line-height:24px;">{escape(titulo)}</h2>'
		              + "".join(f'<p data-cancelacion style="margin:0 0 8px 0;">{escape(x)}</p>' for x in textos))
	if datos["direccion"]:
		partes.append(f'<h2 style="margin:24px 0 4px 0;font-size:18px;line-height:24px;">{escape(t["direccion"])}</h2>'
		              f'<p style="margin:0;">{escape(datos["direccion"])}</p>')
	contacto = [escape(datos["telefono"])] if datos["telefono"] else []
	if datos["correo"]:
		contacto.append(correo.enlace(datos["correo"], f"mailto:{datos['correo']}"))
	if contacto:
		partes.append(f'<h2 style="margin:24px 0 4px 0;font-size:18px;line-height:24px;">{escape(t["contacto"])}</h2>'
		              f'<p style="margin:0;">{" · ".join(contacto)}</p>')
	partes.append(f'<p style="margin-top:24px;">{escape(t["ayuda"])}</p>')
	partes.append(f"<p>{escape(t['despedida'].format(hotel=datos['hotel']))}</p>")
	return t["asunto"].format(hotel=datos["hotel"]), "".join(p for p in partes if p)
