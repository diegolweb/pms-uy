"""El informe del modo contingencia (HR-34): por fecha y tipo de habitación, las libres, lo vendido por plataforma y qué
abrir y qué cerrar en cada extranet, contra lo último que recepción marcó como hecho.

Las libres salen del MISMO cálculo que Kamra empuja a un channel manager (`kamra.channel_manager.ari_snapshot`): las
habitaciones sin reserva asignada de `_available_rooms_raw` menos los cupos de grupo (`_block_hold`), topadas por
`_sellable`, que también descuenta las reservas SIN número de habitación (así llega una reserva de plataforma), con el
bloqueo villa/habitación y las Sellable Units si la propiedad las usa. `_available_rooms_raw` solo no basta: no ve una
reserva sin habitación, y el plan abriría cupos que ya están vendidos.
"""

from __future__ import annotations

from datetime import date

import frappe
from frappe.utils import add_days, getdate, nowdate

from kamra_uy.pure.contingency import (
	Instruction,
	PlanKey,
	PlatformQuota,
	PlatformRow,
	changes,
	config_errors,
	match_platform,
	needs_alert,
	nights,
	notice_hash,
	parse_aliases,
	plan_day,
	plan_from_json,
	plan_to_json,
	split_recipients,
)

SETTINGS = "UY Contingency Settings"
SNAPSHOT = "UY Contingency Snapshot"
STAFF_ROLES = ("Front Desk", "Hotel Admin")
LIVE_STATUSES = ("Confirmed", "Checked In")
DEFAULT_HORIZON = 120

OFF_MESSAGES = {
	"synced": "La sincronización con las plataformas está activa: el modo contingencia está apagado.",
	"disabled": "El modo contingencia está desactivado en su configuración.",
	"unconfigured": "El modo contingencia no está configurado para esta propiedad.",
}
MISSING_QUOTAS = "Faltan los cupos de las plataformas: cárguelos en la configuración del modo contingencia."


# ---------------------------------------------------------------- estado del modo


def settings_for(property: str):
	"""La configuración de la propiedad, o None si no tiene."""
	if not property or not frappe.db.exists(SETTINGS, property):
		return None
	return frappe.get_doc(SETTINGS, property)


def has_channex(property: str) -> bool:
	"""HR-39 activa la conexión de Channex al sincronizar: desde ese momento, el modo se apaga solo."""
	return bool(frappe.db.exists("Channel Manager Connection",
	                             {"property": property, "provider": "Channex", "active": 1}))


def mode_status(property: str, settings=None) -> str:
	"""«active», o por qué está apagado: «synced» (Channex activo), «disabled» o «unconfigured»."""
	if has_channex(property):
		return "synced"
	settings = settings if settings is not None else settings_for(property)
	if settings is None:
		return "unconfigured"
	return "active" if settings.enabled else "disabled"


def is_active(property: str) -> bool:
	"""Activo si su configuración está activada y la propiedad no tiene una conexión de Channex activa."""
	return mode_status(property) == "active"


# ---------------------------------------------------------------- libres y vendidas


def free_by_night(property: str, room_type: str, start: date, days: int) -> list[int]:
	"""Habitaciones libres de un tipo, noche a noche desde `start`: el cálculo por día de `ari_snapshot` de Kamra."""
	from kamra.api import _available_rooms_raw, _block_hold
	from kamra.channel_manager import _sellable
	from kamra.kamra.doctype.reservation.reservation import villa_locked_nights
	from kamra.siu.availability import capacity_by_night, has_active_sius

	end = add_days(start, days)
	use_siu = has_active_sius(property, room_type)
	siu_caps = capacity_by_night(property, room_type, str(start), str(end)) if use_siu else []
	locked = villa_locked_nights(property, room_type, start, end)
	total = frappe.db.count("Room", {"room_type": room_type})
	out = []
	for i in range(days):
		d = add_days(start, i)
		d2 = add_days(d, 1)
		if str(d) in locked:
			out.append(0)
			continue
		hold = _block_hold(property, room_type, d, d2) or 0
		if use_siu:
			base = siu_caps[i] if i < len(siu_caps) else 0
		else:
			base = len(_available_rooms_raw(property, room_type, d, d2))
		free = max(0, base - hold)
		if total:
			free = min(free, _sellable(property, room_type, d, d2))
		out.append(int(free))
	return out


def sold_by_night(property: str, aliases: dict[str, frozenset[str]], start: date, days: int) -> dict:
	"""{(fecha ISO, tipo): {plataforma: reservas}} de las reservas vivas cuyo `channel` es un alias de la plataforma."""
	end = add_days(start, days)
	rows = frappe.db.sql(
		"""SELECT room_type, channel, check_in_date, check_out_date FROM `tabReservation`
		   WHERE property = %(p)s AND status IN %(live)s AND IFNULL(channel, '') != ''
		     AND check_in_date < %(end)s
		     AND GREATEST(check_out_date, DATE_ADD(check_in_date, INTERVAL 1 DAY)) > %(start)s""",
		{"p": property, "live": LIVE_STATUSES, "start": start, "end": end},
		as_dict=True,
	)
	sold: dict = {}
	first, last = getdate(start), getdate(end)
	for r in rows:
		platform = match_platform(r.channel, aliases)
		if not platform:
			continue
		for night in nights(getdate(r.check_in_date), getdate(r.check_out_date)):
			if first <= night < last:
				cell = sold.setdefault((str(night), r.room_type), {})
				cell[platform] = cell.get(platform, 0) + 1
	return sold


# ---------------------------------------------------------------- el plan


def _quotas(settings) -> dict[str, list[PlatformQuota]]:
	by_type: dict[str, list[PlatformQuota]] = {}
	for row in settings.platforms:
		by_type.setdefault(row.room_type, []).append(
			PlatformQuota(row.platform.strip(), int(row.quota or 0), int(row.priority or 1)))
	return by_type


def _aliases(settings) -> dict[str, frozenset[str]]:
	aliases: dict[str, frozenset[str]] = {}
	for row in settings.platforms:
		name = row.platform.strip()
		aliases[name] = aliases.get(name, frozenset()) | parse_aliases(name, row.aliases)
	return aliases


def plan_rows(property: str, settings, start: date, days: int) -> list[dict]:
	"""Una fila por fecha y tipo con cupos, en orden de fecha y de nombre de tipo, sin comparar con nada."""
	quotas = _quotas(settings)
	if not quotas:
		return []
	sold = sold_by_night(property, _aliases(settings), start, days)
	buffer = int(settings.buffer_rooms or 0)
	threshold = int(settings.alert_threshold or 0)
	labels = {rt: frappe.db.get_value("Room Type", rt, "room_type_name") or rt for rt in quotas}
	free = {rt: free_by_night(property, rt, start, days) for rt in quotas}
	rows = []
	for i in range(days):
		d = str(add_days(start, i))
		for rt in sorted(quotas, key=lambda t: (labels[t], t)):
			platforms = quotas[rt]
			cell = sold.get((d, rt), {})
			rows.append({
				"date": d, "room_type": rt, "room_type_name": labels[rt], "free": free[rt][i],
				"sold": {p.name: cell.get(p.name, 0) for p in sorted(platforms, key=lambda p: (p.priority, p.name))},
				"instructions": plan_day(free[rt][i], platforms, buffer),
				"alert": needs_alert(free[rt][i], threshold),
			})
	return rows


def as_plan(rows: list[dict]) -> dict[PlanKey, Instruction]:
	return {(r["date"], r["room_type"], i.platform): i for r in rows for i in r["instructions"]}


def plan_hash(rows: list[dict]) -> str:
	"""Huella del plan que se ve: el botón la manda para no marcar como hecho un plan que cambió entretanto."""
	return notice_hash(sorted(plan_to_json(as_plan(rows)).items()))


def last_applied(property: str) -> dict[PlanKey, Instruction]:
	"""Lo último que recepción marcó como hecho en las extranets (vacío si nunca)."""
	last = frappe.get_all(SNAPSHOT, filters={"property": property}, fields=["plan"],
	                      order_by="applied_on desc, creation desc", limit=1)
	return plan_from_json(last[0].plan) if last else {}


def mark_changes(rows: list[dict], applied: dict[PlanKey, Instruction]) -> int:
	"""Marca `changed` en cada fila que difiere de lo aplicado y devuelve cuántas instrucciones cambiaron."""
	pending = changes(applied, as_plan(rows))
	changed = {(key[0], key[1]) for key, _, _ in pending}
	for r in rows:
		r["changed"] = (r["date"], r["room_type"]) in changed
	return len(pending)


def build_report(property: str, start: date | None = None) -> dict:
	"""El informe de la propiedad, de `start` (hoy) a su horizonte. Apagado: `active` False y sin filas."""
	settings = settings_for(property)
	status = mode_status(property, settings)
	start = getdate(start or nowdate())
	days = int((settings.horizon_days if settings else 0) or DEFAULT_HORIZON)
	result = {"active": status == "active", "reason": status, "property": property, "start": str(start),
	          "days": days, "rows": [], "pending_changes": 0, "missing_quotas": False, "plan_hash": ""}
	if status != "active":
		return result
	result["missing_quotas"] = not settings.platforms
	rows = plan_rows(property, settings, start, days)
	result["rows"] = rows
	result["pending_changes"] = mark_changes(rows, last_applied(property))
	result["plan_hash"] = plan_hash(rows)
	return result


# ---------------------------------------------------------------- configuración


def validate_settings(doc) -> None:
	"""`UY Contingency Settings.validate`: números, filas, tipos de la misma propiedad y correos."""
	rows = [PlatformRow(r.platform or "", r.room_type or "", int(r.quota or 0), int(r.priority or 0))
	        for r in doc.platforms]
	errors = config_errors(int(doc.buffer_rooms or 0), int(doc.alert_threshold or 0),
	                       int(doc.horizon_days if doc.horizon_days is not None else DEFAULT_HORIZON), rows)
	for number, r in enumerate(doc.platforms, start=1):
		if r.room_type and frappe.db.get_value("Room Type", r.room_type, "property") != doc.property:
			errors.append(f"Fila {number} ({r.platform}): el tipo {r.room_type} no es de {doc.property}.")
	for email in split_recipients(doc.recipients):
		if not frappe.utils.validate_email_address(email):
			errors.append(f"«{email}» no es un correo válido.")
	if errors:
		frappe.throw("<br>".join(frappe.utils.escape_html(e) for e in errors), title="Modo contingencia")


def staff_roles_ok() -> bool:
	"""Personal del hotel: Front Desk, Hotel Admin o un administrador (los mismos que `require_roles`)."""
	from kamra.authz import ADMIN

	return bool(set(STAFF_ROLES + tuple(ADMIN)) & set(frappe.get_roles()))


def property_for_user(requested: str | None) -> str | None:
	"""La propiedad pedida, o la única que el usuario puede ver; None si hay que elegir."""
	from kamra.authz import assert_property_access, restricted_properties

	if requested:
		assert_property_access(requested)
		return requested
	allowed = restricted_properties()
	names = sorted(allowed) if allowed is not None else frappe.get_all("Property", pluck="name", order_by="name")
	return names[0] if len(names) == 1 else None

