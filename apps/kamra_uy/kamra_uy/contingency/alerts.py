"""Los avisos del modo contingencia (HR-34): el informe diario y el aviso en cuanto una reserva cambia el plan.

- `daily_report`: lo llama el programador CADA HORA (hooks.py) y solo manda a la hora del informe en la zona de la
  propiedad (las 07:00; `is_daily_report_hour`). Frappe evalúa los cron en la zona del SISTEMA, que puede no ser la del
  hotel: un cron fijo «0 10 * * *» solo acierta si el sitio está en UTC.
- `on_reservation_change`: doc_event de Reservation; encola `recompute` con las fechas de la estancia (las de antes y
  las de ahora, si se movió).
- `recompute`: si alguna instrucción de esas fechas difiere de lo último que recepción marcó como hecho, o alguna fecha
  está en alerta, un correo SOLO con eso. No repite: guarda en caché la huella de lo avisado por fecha.

Un aviso nunca hace fallar una reserva: todo va en `try` y el fallo queda en un Error Log «Contingencia» con la
traza SOLA (`_log_failure`, HR-82): sin `message`, Frappe guarda las variables locales de cada marco.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import frappe
from frappe.utils import add_days, escape_html, get_url, getdate, nowdate

from kamra_uy.contingency import report
from kamra_uy.pure.contingency import (
	alert_text,
	changes,
	group_ranges,
	horizon_text,
	instruction_text,
	is_daily_report_hour,
	notice_hash,
	range_text,
	split_recipients,
)

LOG_TITLE = "Contingencia"
WATCHED = ("property", "room_type", "room", "status", "check_in_date", "check_out_date", "channel")
NOTICE_TTL = 400 * 24 * 3600  # más que el horizonte máximo: una huella vive lo que su fecha


def _log_failure() -> None:
	"""El Error Log de un aviso que falló, sin variables locales (HR-82). `frappe.log_error` sin `message` usa
	`get_traceback(with_context=True)`, que en developer_mode vuelca cada variable de cada marco: la reserva, la
	propiedad y sus fechas. Aquí basta la traza."""
	frappe.log_error(title=LOG_TITLE, message=frappe.get_traceback(with_context=False))


def _notified_key(property: str) -> str:
	return f"uy_contingency_notified:{property}"


def forget_notices(property: str) -> None:
	"""Tras marcar el plan como hecho, lo avisado antes ya no cuenta."""
	frappe.cache.delete_value(_notified_key(property))


def page_url(property: str) -> str:
	return get_url(f"/contingencia?property={quote(property)}")


# ---------------------------------------------------------------- cambios de reserva


def _stays(doc, method: str | None) -> list[tuple[str, str, str]] | None:
	"""(propiedad, entrada, salida) que hay que recalcular; None si el cambio no mueve la disponibilidad."""
	stays = [(doc.get("property"), doc.get("check_in_date"), doc.get("check_out_date"))]
	if method == "on_update":
		if doc.flags.in_insert:
			return None  # after_insert lo cubre
		before = doc.get_doc_before_save()
		if before is not None:
			if all(before.get(f) == doc.get(f) for f in WATCHED):
				return None
			stays.append((before.get("property"), before.get("check_in_date"), before.get("check_out_date")))
	return [s for s in stays if s[0] and s[1] and s[2]]


def on_reservation_change(doc, method=None):
	"""doc_event de Reservation (after_insert, on_update, on_cancel, on_trash)."""
	try:
		stays = _stays(doc, method)
		if not stays:
			return
		windows: dict[str, tuple] = {}
		for property, check_in, check_out in stays:
			ci, co = getdate(check_in), getdate(check_out)
			co = max(co, ci + timedelta(days=1))
			lo, hi = windows.get(property, (ci, co))
			windows[property] = (min(lo, ci), max(hi, co))
		for property, (ci, co) in windows.items():
			if not report.is_active(property):
				continue
			frappe.enqueue(
				"kamra_uy.contingency.alerts.recompute",
				property=property, check_in=str(ci), check_out=str(co),
				# por fechas: con la propiedad sola, una segunda reserva en cola con otras fechas se perdería
				job_id=f"uy-contingency-{property}-{ci}-{co}", deduplicate=True,
				enqueue_after_commit=True, now=frappe.in_test,
			)
	except Exception:
		_log_failure()


def _notice_items(rows: list[dict], applied: dict) -> dict[str, tuple[list, list[str]]]:
	"""Por fecha: (huella, líneas) de lo pendiente frente a lo aplicado y de las alertas."""
	pending = {}
	for key, _before, now in changes(applied, report.as_plan(rows)):
		pending.setdefault((key[0], key[1]), []).append(now)
	out: dict[str, tuple[list, list[str]]] = {}
	for r in rows:
		items, lines = out.setdefault(r["date"], ([], []))
		day = getdate(r["date"])
		for instruction in pending.get((r["date"], r["room_type"]), []):
			items.append([r["room_type"], instruction.platform, instruction.action, instruction.rooms])
			lines.append(instruction_text(instruction, day, r["room_type_name"], r["free"]))
		if r["alert"]:
			items.append(["alert", r["room_type"]])
			lines.append(alert_text(day, r["room_type_name"], r["free"]))
	return out


def recompute(property: str, check_in: str, check_out: str) -> int:
	"""Avisa lo que cambió en esas fechas; devuelve cuántas líneas mandó (0 si nada nuevo o el modo está apagado)."""
	try:
		return _recompute(property, check_in, check_out)
	except Exception:
		_log_failure()
		return 0


def _recompute(property: str, check_in: str, check_out: str) -> int:
	settings = report.settings_for(property)
	if report.mode_status(property, settings) != "active":
		return 0
	today = getdate(nowdate())
	start = max(getdate(check_in), today)
	end = min(getdate(check_out), getdate(add_days(today, int(settings.horizon_days or report.DEFAULT_HORIZON))))
	if start >= end:
		return 0
	rows = report.plan_rows(property, settings, start, (end - start).days)
	key = _notified_key(property)
	fresh: dict[str, str] = {}
	lines: list[str] = []
	for day, (items, day_lines) in sorted(_notice_items(rows, report.last_applied(property)).items()):
		fingerprint = notice_hash(items) if items else ""
		previous = frappe.cache.hget(key, day)
		if fingerprint == (previous or ""):
			continue
		fresh[day] = fingerprint
		lines.extend(day_lines)
	recipients = split_recipients(settings.recipients)
	if lines and not recipients:
		return 0  # sin destinatarios no se guarda la huella: cuando los haya, se avisa
	if lines:
		_send(property, recipients, f"Contingencia: cambios en las extranets ({property})",
		      "Cambió lo que hay que poner en las extranets:", lines)
	for day, fingerprint in fresh.items():
		frappe.cache.hset(key, day, fingerprint)
	if fresh:
		frappe.cache.expire(frappe.cache.make_key(key), NOTICE_TTL)
	return len(lines)


# ---------------------------------------------------------------- informe diario


def daily_report() -> int:
	"""Cada hora (hooks.py); a las 07:00 de la propiedad, el informe a sus destinatarios. Devuelve cuántos mandó."""
	sent = 0
	now = datetime.now(UTC)
	for name in frappe.get_all(report.SETTINGS, filters={"enabled": 1}, pluck="property"):
		try:
			timezone_name = frappe.db.get_value("Property", name, "timezone")
			if not is_daily_report_hour(now, timezone_name):
				continue
			sent += _send_daily(name)
		except Exception:
			_log_failure()
	return sent


def _send_daily(property: str) -> int:
	settings = report.settings_for(property)
	recipients = split_recipients(settings.recipients) if settings else []
	result = report.build_report(property)
	if not result["active"] or not recipients:
		return 0
	lines = []
	if result["missing_quotas"]:
		lines.append(report.MISSING_QUOTAS)
	pending = [r for r in result["rows"] if r["changed"]]
	if pending:
		lines.append(f"Pendiente de poner en las extranets ({result['pending_changes']}):")
		lines += [instruction_text(i, getdate(r["date"]), r["room_type_name"], r["free"])
		          for r in pending for i in r["instructions"]]
	alerts = [alert_text(getdate(r["date"]), r["room_type_name"], r["free"]) for r in result["rows"] if r["alert"]]
	if alerts:
		lines.append("Pocas habitaciones:")
		lines += alerts
	lines.append(horizon_text(getdate(result["start"]), result["days"]))
	lines += [range_text(*rng) for rng in group_ranges(
		[(r["room_type_name"], getdate(r["date"]), tuple(r["instructions"])) for r in result["rows"]])]
	_send(property, recipients, f"Contingencia: informe del día ({property})",
	      "El informe de hoy del modo contingencia:", lines)
	return 1


# ---------------------------------------------------------------- correo


def _send(property: str, recipients: list[str], subject: str, intro: str, lines: list[str]) -> None:
	items = "".join(f"<li>{escape_html(line)}</li>" for line in lines)
	url = page_url(property)
	message = (f"<p>{escape_html(intro)}</p><ul>{items}</ul>"
	           f'<p>El detalle y el botón «Ya lo hice en las extranets», en <a href="{escape_html(url)}">'
	           f"{escape_html(url)}</a>.</p>")
	frappe.sendmail(recipients=recipients, subject=subject, message=message,
	                reference_doctype=report.SETTINGS, reference_name=property)
