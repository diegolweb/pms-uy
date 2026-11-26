"""La agencia de viajes inscripta en el MINTUR con su constancia vigente (HR-236, D-HR-74 pregunta 11).

El estudio (24-11): una agencia que «presenta su constancia vigente» no paga IVA por las noches fuera de la alta
temporada (services/core/config/iva-rules.uy.yaml, `hospedaje_fuera_temporada`, comprador `mintur_agency`); dentro de
ella, el 10 %, como cualquier residente. Cualquier otro RUT, el 10 % todo el año.

Recepción lo guarda en la ficha de la empresa (`Corporate Account`, de Kamra) con dos campos de este módulo: la
constancia adjunta y hasta cuándo vale. La empresa cuenta como agencia solo con los dos y un RUT válido (D-S236-1); la
vigencia se mide noche a noche, la noche del vencimiento incluida (D-S236-2), igual en core (`buyerOf` de
services/core/src/fiscal/iva/engine.ts) que al reservar (`uy_tax.booking_room_rate`).

`constancia_until` es la única que lee esos campos: la usan la factura (`connector.guest_inputs`, que la manda a core
en `buyer.mintur_agency.constancia_valid_until`) y el IVA de cada noche al reservar (`booking.residence`).
"""

from __future__ import annotations

import frappe
from frappe.utils import getdate

from kamra_uy.fiscal import model

MODULE = "Kamra UY"
DOCTYPE = "Corporate Account"
CONSTANCIA = "uy_mintur_constancia"
VALID_UNTIL = "uy_mintur_valid_until"

_DESCRIPTION = ("Solo para una agencia de viajes inscripta en el MINTUR. Con la constancia adjunta y la fecha hasta la "
                "que vale, la agencia no paga IVA por las noches fuera de la temporada alta hasta esa fecha (dentro de "
                "ella, el 10 %). Sin las dos, paga como cualquier empresa.")

FIELDS = {
	DOCTYPE: [
		{"fieldname": CONSTANCIA, "label": "Constancia del MINTUR (agencia de viajes)", "fieldtype": "Attach",
		 "insert_after": "gstin", "module": MODULE, "no_copy": 1, "print_hide": 1, "description": _DESCRIPTION},
		{"fieldname": VALID_UNTIL, "label": "Constancia del MINTUR vigente hasta", "fieldtype": "Date",
		 "insert_after": CONSTANCIA, "module": MODULE, "no_copy": 1, "print_hide": 1, "description": _DESCRIPTION},
	],
}


def ensure_fields() -> None:
	"""Crea o pone al día los dos campos de la Corporate Account (after_install, after_migrate). Idempotente."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(FIELDS, update=True)


def _has_fields() -> bool:
	meta = frappe.get_meta(DOCTYPE)
	return meta.has_field(CONSTANCIA) and meta.has_field(VALID_UNTIL)


def constancia_until(company: str | None) -> str | None:
	"""La fecha (AAAA-MM-DD) hasta la que vale la constancia del MINTUR de la empresa, si tiene RUT válido, la
	constancia adjunta y la fecha. Si le falta cualquiera de las tres (o no existe), None: paga como cualquier empresa."""
	if not company or not _has_fields():
		return None
	row = frappe.db.get_value(DOCTYPE, company, ["gstin", CONSTANCIA, VALID_UNTIL], as_dict=True)
	if not row or not (row.get(CONSTANCIA) or "").strip() or not row.get(VALID_UNTIL):
		return None
	if not model.valid_rut("".join(ch for ch in (row.get("gstin") or "") if ch.isdigit())):
		return None
	return getdate(row.get(VALID_UNTIL)).isoformat()
