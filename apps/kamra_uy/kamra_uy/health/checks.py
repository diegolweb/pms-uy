"""Las señales de salud de Kamra (HR-21), las que lee /api/method/kamra_uy.health.status y vigila el vigía.

Cada señal da {"status": "ok" | "fail" | "off", "detail": texto para el fundador}. «off» es «no aplica aquí» (sin
conexión de Channex, sin facturación electrónica, sin core): nunca avisa. Los umbrales salen de site_config.json
(`bench --site S set-config -p hr_health_… N`), con los valores de DEFAULTS:

- programador: el latido de kamra_uy/health/beat.py tiene más de 1,75 ticks del programador de Frappe (7 min con su
  tick de 4 min; nunca menos de 5; o hr_health_scheduler_minutes), o el programador está apagado (desactivado, en
  pausa o en mantenimiento). Con el vigía mirando cada minuto, parar el programador avisa en 4-9 min.
- revisiones: una revisión de Channex (Channel Booking Revision, HR-15) de una conexión activa lleva más de 15 min sin
  acusar (Received, Applied o Failed): una reserva que Kamra no tomó, o que tomó y Channex seguirá mandando.
- ari: Channex rechazó un envío de disponibilidad o tarifas (HR-16): filas «Failed» de Channel ARI Outbox de las
  últimas 24 h (el barrido de cada noche lo vuelve a mandar; una sincronización completa enviada después las da por
  arregladas) o el último envío de la conexión en «FAILED».
- cfe: en Kamra, filas de UY CFE «Pending» de más de 30 min (no llegaron a core) o «Needs attention» (Finanzas tiene
  que mirarlas); en core (listCfe, la clave de la propiedad con el alcance `read`), CFE en cola de más de 30 min o en
  contingencia. Core que no contesta también es rojo: no se puede facturar.
- errores: 5 o más Error Log en los últimos 60 min (con los títulos más repetidos, filtrados).
- disco: la carpeta del sitio (el volumen `sites`) está al 90 % o más.
- core: /health de core (la red interna, hr_core_base_url) no contesta 200 con status ok.

Ningún detalle lleva datos de huéspedes: lo que viene de un texto libre (títulos de errores, el último envío a
Channex) pasa por kamra_uy/health/scrub.py.
"""

from __future__ import annotations

import hmac
import math
import shutil
from collections import Counter
from datetime import UTC, datetime, timedelta, timezone

import frappe
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from kamra_uy.core.client import CoreError
from kamra_uy.fiscal import connector
from kamra_uy.health import beat, scrub

CACHE_KEY = "kamra_uy:health:report"
CACHE_SECONDS = 30
TOKEN_HEADER = "X-HR-Health-Token"
TOKEN_CONF = "hr_health_token"
DEFAULTS = {
	"hr_health_revision_minutes": 15,
	"hr_health_ari_hours": 24,
	"hr_health_cfe_minutes": 30,
	"hr_health_errors_minutes": 60,
	"hr_health_errors_max": 5,
	"hr_health_disk_percent": 90,
	"hr_health_core_timeout": 5,
}
CHANNEX = "Channex"


def setting(key: str) -> int:
	value = frappe.conf.get(key)
	return DEFAULTS[key] if value in (None, "") else cint(value)


def _ok(detail: str) -> dict:
	return {"status": "ok", "detail": detail}


def _fail(detail: str) -> dict:
	return {"status": "fail", "detail": detail}


def _off(detail: str) -> dict:
	return {"status": "off", "detail": detail}


def _n(count: int, one: str, many: str) -> str:
	return f"{count} {one if count == 1 else many}"


def _minutes(now: datetime, then) -> int:
	return int((now - get_datetime(then)).total_seconds() // 60)


# ── programador ─────────────────────────────────────────────────────────────────────────────────────────────────
def _scheduler_inactive() -> bool:
	from frappe.utils.scheduler import is_scheduler_inactive

	return is_scheduler_inactive(verbose=False)


def _scheduler_tick() -> int:
	"""Cada cuántos segundos encola Frappe los trabajos programados (scheduler_tick_interval; 240 por defecto en
	Frappe 16): un cron «* * * * *» corre una vez por tick, no cada minuto."""
	from frappe.utils.scheduler import get_scheduler_tick

	return get_scheduler_tick()


def scheduler_limit_minutes() -> int:
	"""hr_health_scheduler_minutes, o 1,75 ticks (7 min con el tick de 4 min), nunca menos de 5: un latido que llega
	un tick tarde no es una alarma, y parar el programador sigue avisando en menos de 10 min."""
	explicit = frappe.conf.get("hr_health_scheduler_minutes")
	if explicit not in (None, ""):
		return cint(explicit)
	return max(5, math.ceil(_scheduler_tick() * 1.75 / 60))


def programador(now: datetime) -> dict:
	if _scheduler_inactive():
		return _fail("el programador está apagado (desactivado, en pausa o en mantenimiento)")
	last = beat.last()
	if last is None:
		return _fail("el programador todavía no dio ningún latido")
	age = (now - last).total_seconds()
	limit = scheduler_limit_minutes()
	if age > limit * 60:
		return _fail(f"el último latido fue hace {int(age // 60)} min (más de {limit} min): el programador o los "
		             "trabajadores están parados")
	return _ok(f"último latido hace {max(0, int(age))} s")


# ── Channex ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _channex_connections(fields=("name",)) -> list:
	return frappe.get_all("Channel Manager Connection", filters={"provider": CHANNEX, "active": 1},
	                      fields=list(fields), order_by="name asc")


def revisiones(now: datetime) -> dict:
	conns = [c.name for c in _channex_connections()]
	if not conns:
		return _off("sin conexión de Channex activa")
	limit = setting("hr_health_revision_minutes")
	rows = frappe.get_all("Channel Booking Revision", filters={
		"connection": ["in", conns], "status": ["!=", "Acked"],
		"creation": ["<", add_to_date(now, minutes=-limit)]}, fields=["creation"], order_by="creation asc", limit=500)
	if rows:
		return _fail(f"{_n(len(rows), 'revisión', 'revisiones')} de Channex sin acusar desde hace más de {limit} min "
		             f"(la más antigua, hace {_minutes(now, rows[0].creation)} min)")
	return _ok(f"ninguna revisión de Channex lleva más de {limit} min sin acusar")


def ari(now: datetime) -> dict:
	conns = _channex_connections(("name", "last_push_status"))
	if not conns:
		return _off("sin conexión de Channex activa")
	names = [c.name for c in conns]
	hours = setting("hr_health_ari_hours")
	since = add_to_date(now, hours=-hours)
	failed = frappe.get_all("Channel ARI Outbox", filters={"connection": ["in", names], "status": "Failed",
	                                                       "modified": [">=", since]},
	                        fields=["connection", "modified"], limit=500)
	# una sincronización completa enviada después de la fila rechazada la deja arreglada
	full = {}
	for row in frappe.get_all("Channel ARI Outbox", filters={"connection": ["in", names], "status": "Sent", "full": 1,
	                                                         "sent_at": [">=", since]},
	                          fields=["connection", "sent_at"]):
		full[row.connection] = max(full.get(row.connection, row.sent_at), row.sent_at)
	failed = [r for r in failed if not (r.connection in full and get_datetime(full[r.connection]) > get_datetime(r.modified))]
	problems = []
	if failed:
		problems.append(f"Channex rechazó {_n(len(failed), 'envío', 'envíos')} de disponibilidad o tarifas en las "
		                f"últimas {hours} h")
	for c in conns:
		status = c.last_push_status or ""
		if status.startswith("FAILED"):
			problems.append(f"el último envío de {c.name} falló ({scrub.text(status[len('FAILED: '):])[:140]})")
	if problems:
		return _fail("; ".join(problems))
	return _ok("Channex aceptó los últimos envíos")


# ── CFE ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def _fiscal_properties() -> list[str]:
	props = frappe.get_all("Payment Gateway Settings", filters={"gateway": connector.GATEWAY}, pluck="property")
	return [p for p in dict.fromkeys(props) if p and connector.is_uruguay(p)]


def _core_instant(value) -> datetime | None:
	try:
		return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
	except ValueError:
		return None


def _core_cfes(prop: str, limit_minutes: int) -> tuple[int, int, int]:
	"""(en cola de más de N min, en contingencia, fuera de plazo) de la propiedad, según core (listCfe)."""
	_code, client = connector.client_for(prop, timeout=setting("hr_health_core_timeout"))
	now = datetime.now(UTC)
	queued = contingency = overdue = 0
	for status in ("queued", "contingency"):
		for cfe in client.get(f"/v1/fiscal/cfe?status={status}&limit=200").get("items") or []:
			created = _core_instant(cfe.get("created_at"))
			overdue += bool(cfe.get("overdue"))
			if status == "contingency":
				contingency += 1
			elif created is None or now - created > timedelta(minutes=limit_minutes):
				queued += 1
	return queued, contingency, overdue


def cfe(now: datetime) -> dict:
	props = _fiscal_properties()
	if not props and not frappe.db.count("UY CFE"):
		return _off("sin facturación electrónica configurada")
	limit = setting("hr_health_cfe_minutes")
	cutoff = add_to_date(now, minutes=-limit)
	pending = frappe.db.count("UY CFE", {"status": "Pending", "creation": ["<", cutoff]})
	attention = frappe.db.count("UY CFE", {"status": "Needs attention"})
	problems = []
	if pending:
		problems.append(f"Kamra: {pending} sin mandar a core desde hace más de {limit} min")
	if attention:
		problems.append(f"Kamra: {attention} a revisar por Finanzas (UY CFE «Needs attention»)")
	for prop in props:
		try:
			queued, contingency, overdue = _core_cfes(prop, limit)
		except CoreError as exc:
			if exc.code == "not_configured":
				continue
			problems.append(f"no pude preguntar a core por los CFE de {prop}: {exc.code} ({exc.status or 'sin respuesta'})")
			continue
		if queued:
			problems.append(f"core ({prop}): {queued} en cola desde hace más de {limit} min")
		if contingency:
			late = f", {overdue} fuera de plazo" if overdue else ""
			problems.append(f"core ({prop}): {contingency} en contingencia{late}")
	if problems:
		return _fail("; ".join(problems))
	return _ok(f"ningún CFE pendiente de más de {limit} min")


# ── errores, disco y core ───────────────────────────────────────────────────────────────────────────────────────
def errores(now: datetime) -> dict:
	minutes, maximum = setting("hr_health_errors_minutes"), setting("hr_health_errors_max")
	titles = frappe.get_all("Error Log", filters={"creation": [">=", add_to_date(now, minutes=-minutes)]},
	                        pluck="method", limit=1000)
	count = len(titles)
	text = f"{_n(count, 'error', 'errores')} en los últimos {minutes} min"
	if count < maximum:
		return _ok(text)
	top = Counter(scrub.text(t)[:80] for t in titles).most_common(3)
	return _fail(f"{text} (los más repetidos: {', '.join(f'«{t}» {n} veces' for t, n in top)})")


def _disk_usage(path: str):
	return shutil.disk_usage(path)


def disco(now: datetime) -> dict:
	total, used, free = _disk_usage(frappe.get_site_path())
	pct = round(used * 100 / total) if total else 100
	text = f"usado {pct} % ({free / 2**30:.1f} GB libres)"
	return _fail(text) if pct >= setting("hr_health_disk_percent") else _ok(text)


def core(now: datetime) -> dict:
	base = (frappe.conf.get("hr_core_base_url") or "").strip().rstrip("/")
	if not base:
		return _off("sin hr_core_base_url")
	session = connector._session()
	if session is None:
		import requests

		session = requests.Session()
	try:
		resp = session.request("GET", f"{base}/health", timeout=setting("hr_health_core_timeout"))
		body = resp.json()
	except Exception as exc:  # red, tiempo o una respuesta que no es JSON
		return _fail(f"core no responde ({type(exc).__name__})")
	if resp.status_code == 200 and body.get("status") == "ok":
		return _ok(f"core responde (versión {body.get('version') or '?'})")
	return _fail(f"core contesta {resp.status_code}: base de datos {body.get('db', '?')}, cola {body.get('queue', '?')}")


CHECKS = {"programador": programador, "revisiones": revisiones, "ari": ari, "cfe": cfe, "errores": errores,
          "disco": disco, "core": core}


# ── el informe ──────────────────────────────────────────────────────────────────────────────────────────────────
def report(now: datetime | None = None) -> dict:
	"""Todas las señales. Una comprobación que se rompe es una señal roja (con el tipo del error), nunca un 500."""
	now = now or now_datetime()
	signals = {}
	for name, check in CHECKS.items():
		try:
			signals[name] = check(now)
		except Exception as exc:
			frappe.logger("kamra_uy.health").exception(f"health check {name}")
			signals[name] = _fail(f"no pude comprobarlo ({type(exc).__name__})")
	failing = any(s["status"] == "fail" for s in signals.values())
	return {"status": "fail" if failing else "ok", "checked_at": now.isoformat(timespec="seconds"), "signals": signals}


def cached_report() -> dict:
	"""El informe de los últimos CACHE_SECONDS: el endpoint lo pide cualquiera y cada informe pregunta a core."""
	cached = frappe.cache.get_value(CACHE_KEY)
	if cached:
		return cached
	fresh = report()
	frappe.cache.set_value(CACHE_KEY, fresh, expires_in_sec=CACHE_SECONDS)
	return fresh


def may_see_details() -> bool:
	"""El detalle, solo con el token de site_config (cabecera X-HR-Health-Token) o con sesión de System Manager."""
	token = str(frappe.conf.get(TOKEN_CONF) or "")
	given = str(frappe.get_request_header(TOKEN_HEADER) or "")
	if token and given and hmac.compare_digest(given.encode(), token.encode()):
		return True
	return frappe.session.user != "Guest" and "System Manager" in frappe.get_roles()


def public_view(full: dict) -> dict:
	"""Lo que ve cualquiera (un monitor externo, p. ej.): el estado de cada señal, sin detalle."""
	return {"status": full["status"], "checked_at": full["checked_at"],
	        "signals": {k: {"status": v["status"]} for k, v in full["signals"].items()}}
