"""El documento de quien pagó por Mercado Pago (HR-360): llega en `payment.approved` (`payer` del contrato, opcional) y
queda en la reserva, para que la seña retenida de un huésped sin documento (fiscal/forfeit.py) salga con él.

Es un dato personal: se guarda con `db.set_value` (sin historial de cambios) y nunca va a un log, al Error Log, al aviso
guardado (UY Core Event) ni a un correo. El documento del huésped, si está cargado, manda siempre sobre este. Sin import
de frappe arriba: fiscal/forfeit.py lo importa y lo puro se prueba sin él (tests/unit/fiscal/test_forfeit.py).
"""

from __future__ import annotations

MODULE = "Kamra UY"
TYPE_FIELD = "uy_payer_doc_type"
NUMBER_FIELD = "uy_payer_doc_number"
# los tipos de `PaymentPayer` del contrato (services/core/openapi.yaml)
DOC_TYPES = ("ci", "rut", "dni", "passport", "other")
DOC_NUMBER_MAX = 40
# CI y RUT son de Uruguay; del DNI, el pasaporte y «Otro», Mercado Pago no da el país, y el país de un documento nunca
# se supone (HR-346): esos no sirven de comprador
DOC_COUNTRY = {"ci": "UY", "rut": "UY"}
DEPOSIT = "deposit"

FIELDS = {
	"Reservation": [
		{"fieldname": TYPE_FIELD, "label": "Tipo de documento de quien pagó", "fieldtype": "Select",
		 "options": "\n".join(["", *DOC_TYPES]), "read_only": 1, "no_copy": 1, "print_hide": 1,
		 "insert_after": "uy_declared_residence", "module": MODULE},
		{"fieldname": NUMBER_FIELD, "label": "Documento de quien pagó", "fieldtype": "Data", "length": DOC_NUMBER_MAX,
		 "read_only": 1, "no_copy": 1, "print_hide": 1, "insert_after": TYPE_FIELD, "module": MODULE,
		 "description": "Lo da Mercado Pago con el pago. Si el huésped no tiene su documento cargado, el comprobante de la "
		                "seña retenida sale con este."},
	],
}


def ensure_fields() -> None:
	"""Crea o pone al día los campos de la Reservation (after_install, after_migrate). Idempotente."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(FIELDS, update=True)


def payer_of(event: dict) -> dict | None:
	"""El `payer` de un `payment.approved` ({doc_type, doc_number}), o None si no viene o no tiene la forma del contrato.
	Uno mal formado no para el cobro: se ignora."""
	payer = event.get("payer")
	if not isinstance(payer, dict):
		return None
	kind, number = payer.get("doc_type"), payer.get("doc_number")
	if kind not in DOC_TYPES or not isinstance(number, str):
		return None
	number = number.strip()
	if not number or len(number) > DOC_NUMBER_MAX:
		return None
	return {"doc_type": kind, "doc_number": number}


def remember(folio: str, event: dict) -> str | None:
	"""Deja en la reserva del folio el documento de quien pagó. El de una seña (`purpose: deposit`) sustituye al que
	hubiera; el de otro cobro, solo si no había ninguno. Devuelve la reserva si lo guardó."""
	import frappe

	payer = payer_of(event)
	reservation = frappe.db.get_value("Folio", folio, "reservation") if payer else None
	if not reservation or not frappe.get_meta("Reservation").has_field(NUMBER_FIELD):
		return None
	if event.get("purpose") != DEPOSIT and frappe.db.get_value("Reservation", reservation, NUMBER_FIELD):
		return None
	frappe.db.set_value("Reservation", reservation, {TYPE_FIELD: payer["doc_type"], NUMBER_FIELD: payer["doc_number"]},
	                    update_modified=False)
	return reservation


def document(res) -> dict | None:
	"""El documento de quien pagó, en el formato del comprador del CFE ({doc_type, country, doc_number}), o None si no
	hay o su país no se sabe."""
	kind = res.get(TYPE_FIELD)
	number = (res.get(NUMBER_FIELD) or "").strip()
	country = DOC_COUNTRY.get(kind or "")
	if not number or not country:
		return None
	return {"doc_type": kind, "country": country, "doc_number": number}
