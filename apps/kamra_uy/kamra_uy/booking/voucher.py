"""El código de descuento de la reserva en línea (HR-377): los Vales de Kamra (Discount Voucher, «Ingresos → Vales»).

Kamra ya aplica el vale en la cotización (search_stay) y en la reserva (book, que lo vuelve a validar y gasta un uso
bajo cerrojo: kamra.pricing.consume_voucher_use). Aquí, lo que le faltaba a la web del hotel:
  · check_voucher sustituye a kamra.public_api.check_voucher (hooks.override_whitelisted_methods) y contesta un MOTIVO
    (kamra_uy/pure/voucher.py), no una frase en inglés: la web lo dice con sus textos (`vale.error.*`). Tope de Frappe
    por IP, 30 por hora (la web la llama al pulsar «Aplicar» y al volver al paso 2 con un cupón puesto).
  · normalize: el código se guarda en mayúsculas y sin espacios. Kamra lo busca así (voucher_code.strip().upper()) y
    nada lo normalizaba al guardarlo: un vale escrito «verano10» en «Ingresos → Vales» no casaba nunca.
  · on_reservation_update: la reserva web que caduca sin pagar (kamra.reservation_state.expire_holds) devuelve el uso
    del vale; si no, un vale de 1 uso se gastaba con un intento de pago abandonado.
"""

from __future__ import annotations

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils import date_diff, getdate, nowdate

from kamra_uy.pure.voucher import motivo

FIELDS = ["disabled", "valid_from", "valid_to", "max_uses", "times_used", "min_nights"]


@frappe.whitelist(allow_guest=True)
@rate_limit(limit=30, seconds=3600)
def check_voucher(property: str, code: str = "", check_in_date: str | None = None,
                  check_out_date: str | None = None, **kwargs) -> dict:
	"""{"ok": true, "code": "VERANO10"} | {"ok": false, "reason": "vacio" | "no-existe" | "inactivo" | "aun-no" |
	"vencido" | "agotado" | "noches"} (+ "date" en aun-no y vencido, + "min_nights" en noches). La web traduce el
	motivo. Lleva también el "message" de Kamra: la página /book de Kamra (PublicBooking.tsx) llama a esta misma ruta con
	`nights` y enseña esa frase; sin fechas, cuenta `nights` (o una noche)."""
	from kamra import public_api

	code = (code or "").strip().upper()
	if check_in_date and check_out_date:
		noches = max(date_diff(check_out_date, check_in_date), 1)
	else:
		noches = max(int(kwargs.get("nights") or 1), 1)
	message = public_api.check_voucher(property, code, noches).get("message")
	if not code:
		return {"ok": False, "reason": "vacio", "message": message}
	v = frappe.db.get_value("Discount Voucher", {"property": property, "voucher_code": code}, FIELDS, as_dict=True)
	why = motivo(dict(v) if v else None, getdate(nowdate()), noches)
	if why:
		return {"ok": False, **why, "message": message}
	return {"ok": True, "code": code, "message": message}


def normalize(doc, method=None) -> None:
	"""Discount Voucher.validate: el código, en mayúsculas y sin espacios alrededor (como lo busca Kamra)."""
	doc.voucher_code = (doc.voucher_code or "").strip().upper()


def on_reservation_update(doc, method=None) -> None:
	"""Reservation.on_update: la reserva con vale que caduca sin pagar (expire_holds: Held o Pending Payment →
	Cancelled con HOLD_EXPIRY_NOTE) devuelve su uso. El cambio de estado ocurre una vez: la devolución, también."""
	from kamra.reservation_state import HOLD_EXPIRY_NOTE

	if not doc.get("voucher") or doc.status != "Cancelled" or doc.get("cancellation_note") != HOLD_EXPIRY_NOTE:
		return
	before = doc.get_doc_before_save()
	if not before or before.status not in ("Held", "Pending Payment"):
		return
	frappe.db.sql(
		"""UPDATE `tabDiscount Voucher` SET times_used = GREATEST(COALESCE(times_used, 0) - 1, 0) WHERE name = %s""",
		doc.voucher)
