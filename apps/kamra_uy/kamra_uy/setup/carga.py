"""La carga de la planilla del hotel en Kamra (HR-33): habitaciones, tipos, temporadas con precio y el cobro de la
propiedad; y, desde HR-290, su retención, horas de entrada y salida, cancelación, no-show y mascotas, y la capacidad de
cada tipo en personas. Idempotente: la planilla manda y una segunda corrida no cambia nada (claves en `plan.py`).

Primero la PRUEBA, que no escribe y dice qué crearía, cambiaría, dejaría igual o retiraría:

	bench --site <sitio> execute kamra_uy.setup.carga.carga \\
		--kwargs '{"planilla": "/ruta/planilla.xlsx", "propiedad": "<nombre de la propiedad>"}'

y, si sale «lista para cargar», la misma orden con `"aplicar": 1`. Todo o nada: si algo falla a medias, no queda
nada escrito. La planilla se lee fresca en cada corrida y no se copia a ningún sitio; solo se abren sus pestañas
«Habitaciones», «Temporadas y precios» y «Seña y cancelación» (`planilla.PESTANAS`).

Las reservas ya tomadas (la pestaña «Reservas desde 15-nov», con datos personales) se cargan solo si se pide, con
`"reservas": 1` en la prueba y en la carga. Entran como entraría la misma reserva por Channex (`_apply_event` de
kamra.channel_manager): el huésped por su teléfono o correo, Confirmed, sin habitación si la planilla no la dice, con
su `ota_ref` y, si la planilla dice lo que se cobró, ese precio fijado (`lock_channel_rates`) y la comisión de la
plataforma; sin total, la precia Kamra con las temporadas. No se registra ningún pago (lo que el huésped ya pagó se
cobra en recepción, con su comprobante), y la carga no escribe a ningún huésped. El informe no copia nombres, teléfonos
ni correos. La pestaña «Personal» no se abre nunca: los usuarios los crea el fundador en Kamra (D-HR-65).
"""

import frappe
from frappe.utils import getdate, nowdate

from kamra_uy.setup import plan as plan_mod
from kamra_uy.setup import planilla as planilla_mod

_CAMPOS = {
	"Property": [
		"name", "country", "currency", "timezone", "booking_payment_mode", "deposit_pct", "hold_minutes",
		"checkin_time", "checkout_time", "free_cancel_days", "cancellation_fee", "no_show_charge", "pets_allowed",
		"security_deposit_amount",
	],
	"Room Type": [
		"name", "room_type_name", "room_type_code", "base_price", "adults_capacity", "base_occupancy",
		"children_capacity", "max_total_occupants", "children_within_base_occupancy", "child_price", "extra_adult_price",
	],
	"Room": ["name", "room_number", "room_type", "floor", "notes"],
	"Season": [
		"name", "season_name", "room_type", "start_date", "end_date", "adjustment_type", "adjustment_value",
		"minimum_nights", "advance_percent", "full_payment", "disabled", "priority",
	],
}
_RESERVA = ["name", "status", "ota_ref", "idempotency_key", "room_type", "room", "check_in_date", "check_out_date",
            "adults", "children"]


def estado(propiedad: str, reservas: bool = False, hoy=None) -> plan_mod.Estado:
	"""Lo que Kamra tiene hoy de la propiedad, en diccionarios planos para `plan.planea`. Con `reservas`, también sus
	reservas con clave de plataforma o de la carga y las que ocupan habitación de hoy en adelante."""
	prop = None
	if frappe.db.exists("Property", propiedad):
		prop = dict(frappe.db.get_value("Property", propiedad, _CAMPOS["Property"], as_dict=True))

	def todos(doctype: str) -> list[dict]:
		return [dict(r) for r in frappe.get_all(doctype, filters={"property": propiedad}, fields=_CAMPOS[doctype],
		                                        order_by="creation asc")]

	con_clave, ocupadas = [], []
	if prop and reservas:
		con_clave = [dict(r) for r in frappe.get_all(
			"Reservation", filters={"property": propiedad}, fields=_RESERVA, order_by="creation asc",
			or_filters={"ota_ref": ("is", "set"), "idempotency_key": ("like", plan_mod.CLAVE_DIRECTA + "%")},
		)]
		ocupadas = [dict(r) for r in frappe.get_all(
			"Reservation", fields=_RESERVA, order_by="creation asc",
			filters={"property": propiedad, "status": ("in", plan_mod.VIVAS),
			         "check_out_date": (">", hoy or getdate(nowdate()))},
		)]
	return plan_mod.Estado(
		propiedad=prop,
		reservas=frappe.db.count("Reservation", {"property": propiedad}) if prop else 0,
		tipos=todos("Room Type") if prop else [],
		habitaciones=todos("Room") if prop else [],
		temporadas=todos("Season") if prop else [],
		tramos_cancelacion=frappe.db.count(
			"Cancellation Tier", {"parent": propiedad, "parenttype": "Property"}
		) if prop else 0,
		reservas_con_clave=con_clave,
		ocupadas=ocupadas,
	)


def _guarda(doctype: str, nombre: str, valores: dict) -> None:
	doc = frappe.get_doc(doctype, nombre)
	doc.update(valores)
	doc.save(ignore_permissions=True)


def _reserva(propiedad: str, v: dict, tipos: dict) -> str:
	"""Una reserva nueva de la planilla, insertada como `channel_manager._apply_event` inserta una reserva de Channex."""
	from kamra import channel_manager as cm

	habitacion = None
	if v.get("habitacion"):
		habitacion = frappe.db.get_value("Room", {"property": propiedad, "room_number": v["habitacion"]})
	canal = cm.canonical_channel(v.get("channel"))
	doc = frappe.get_doc({
		"doctype": "Reservation",
		"property": propiedad,
		"guest": cm._find_or_create_guest(v["huesped"], v.get("telefono") or "", v.get("correo") or "", propiedad),
		"room_type": tipos[v["room_type"]],
		"room": habitacion,
		"check_in_date": v["check_in_date"],
		"check_out_date": v["check_out_date"],
		"adults": v["adults"],
		"children": v["children"],
		"source": v["source"],
		"status": "Confirmed",
		"channel": canal,
		"ota_ref": v.get("ota_ref"),
		"idempotency_key": v.get("idempotency_key"),
		"special_requests": v.get("notas"),
		# lo que ya se cobró se respeta, como el precio de una plataforma; sin total, Kamra la precia
		"auto_price": 0 if v.get("total") else 1,
	})
	if v.get("total"):
		doc.amount_after_tax = v["total"]
		cm.lock_channel_rates(doc, v["total"])
	doc.insert(ignore_permissions=True)
	if canal:
		cm.set_ota_commission(doc, cm.channel_commission_pcts().get(canal))
	return doc.name


def aplica(pl: plan_mod.Plan) -> None:
	"""Escribe el plan en orden (propiedad, tipos, habitaciones, temporadas y reservas). Sin commit: lo hace quien llama
	(`bench execute` al terminar bien); si algo falla, se deshace todo lo de esta carga."""
	tipos = {a.tipo: a.nombre for a in pl.acciones if a.doctype == "Room Type"}
	frappe.db.savepoint("hr33_carga")
	try:
		for a in pl.acciones:
			if a.op == "igual":
				continue
			if a.doctype == "Reservation":
				_reserva(pl.propiedad, a.valores, tipos)
				continue
			valores = dict(a.valores)
			if "room_type" in valores:
				valores["room_type"] = tipos[valores["room_type"]]
			if a.op == "crear":
				extra = {"priority": 0} if a.doctype == "Season" else {}
				doc = frappe.get_doc({"doctype": a.doctype, "property": pl.propiedad, **extra, **valores})
				doc.insert(ignore_permissions=True)
				if a.doctype == "Room Type":
					tipos[a.tipo] = doc.name
			else:
				_guarda(a.doctype, a.nombre, valores)
	except Exception:
		frappe.db.rollback(save_point="hr33_carga")
		raise
	frappe.clear_document_cache("Property", pl.propiedad)


def carga(planilla: str, propiedad: str, aplicar: int = 0, reservas: int = 0) -> dict:
	"""Lee la planilla, la compara con Kamra, imprime el informe y, con `aplicar`, escribe. Devuelve el recuento. Con
	`reservas`, también la pestaña de las reservas ya tomadas."""
	con_reservas = bool(int(reservas or 0))
	hoy = getdate(nowdate())
	p = planilla_mod.lee(planilla, reservas=con_reservas)
	pl = plan_mod.planea(p, estado(propiedad, con_reservas, hoy), propiedad, hoy=hoy)
	aplicada = bool(int(aplicar or 0)) and not pl.errores
	if aplicada:
		aplica(pl)
	print(pl.texto(aplicado=aplicada))
	return {
		"aplicada": int(aplicada),
		"errores": len(pl.errores),
		"avisos": len(pl.avisos),
		"recuento": pl.recuento(),
		"planilla": p.recuento(),
	}
