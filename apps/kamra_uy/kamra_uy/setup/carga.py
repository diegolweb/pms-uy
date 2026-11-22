"""La carga de la planilla del hotel en Kamra (HR-33): habitaciones, tipos, temporadas con precio y el cobro de la
propiedad. Idempotente: la planilla manda y una segunda corrida no cambia nada (claves en `plan.py`).

Primero la PRUEBA, que no escribe y dice qué crearía, cambiaría, dejaría igual o retiraría:

	bench --site <sitio> execute kamra_uy.setup.carga.carga \\
		--kwargs '{"planilla": "/ruta/planilla.xlsx", "propiedad": "<nombre de la propiedad>"}'

y, si sale «lista para cargar», la misma orden con `"aplicar": 1`. Todo o nada: si algo falla a medias, no queda
nada escrito. La planilla se lee fresca en cada corrida y no se copia a ningún sitio; solo se abren sus pestañas
«Habitaciones» y «Temporadas y precios» (`planilla.PESTANAS`).
"""

import frappe

from kamra_uy.setup import plan as plan_mod
from kamra_uy.setup import planilla as planilla_mod

_CAMPOS = {
	"Property": ["name", "country", "currency", "timezone", "booking_payment_mode", "deposit_pct", "hold_minutes"],
	"Room Type": [
		"name", "room_type_name", "room_type_code", "base_price", "adults_capacity", "base_occupancy",
		"children_capacity",
	],
	"Room": ["name", "room_number", "room_type", "floor", "notes"],
	"Season": [
		"name", "season_name", "room_type", "start_date", "end_date", "adjustment_type", "adjustment_value",
		"minimum_nights", "advance_percent", "full_payment", "disabled", "priority",
	],
}


def estado(propiedad: str) -> plan_mod.Estado:
	"""Lo que Kamra tiene hoy de la propiedad, en diccionarios planos para `plan.planea`."""
	prop = None
	if frappe.db.exists("Property", propiedad):
		prop = dict(frappe.db.get_value("Property", propiedad, _CAMPOS["Property"], as_dict=True))

	def todos(doctype: str) -> list[dict]:
		return [dict(r) for r in frappe.get_all(doctype, filters={"property": propiedad}, fields=_CAMPOS[doctype],
		                                        order_by="creation asc")]

	return plan_mod.Estado(
		propiedad=prop,
		reservas=frappe.db.count("Reservation", {"property": propiedad}) if prop else 0,
		tipos=todos("Room Type") if prop else [],
		habitaciones=todos("Room") if prop else [],
		temporadas=todos("Season") if prop else [],
	)


def _guarda(doctype: str, nombre: str, valores: dict) -> None:
	doc = frappe.get_doc(doctype, nombre)
	doc.update(valores)
	doc.save(ignore_permissions=True)


def aplica(pl: plan_mod.Plan) -> None:
	"""Escribe el plan en orden (propiedad, tipos, habitaciones, temporadas). Sin commit: lo hace quien llama
	(`bench execute` al terminar bien); si algo falla, se deshace todo lo de esta carga."""
	tipos = {a.tipo: a.nombre for a in pl.acciones if a.doctype == "Room Type"}
	frappe.db.savepoint("hr33_carga")
	try:
		for a in pl.acciones:
			if a.op == "igual":
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


def carga(planilla: str, propiedad: str, aplicar: int = 0) -> dict:
	"""Lee la planilla, la compara con Kamra, imprime el informe y, con `aplicar`, escribe. Devuelve el recuento."""
	p = planilla_mod.lee(planilla)
	pl = plan_mod.planea(p, estado(propiedad), propiedad)
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
