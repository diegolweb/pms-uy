#!/usr/bin/env python3
"""El vigía de Kamra (HR-21): mira el sistema desde FUERA y avisa al fundador (correo o WhatsApp) y al Slack del hub.

Fuera de Kamra a propósito: si Kamra se cae o su programador se para, quien avisa no puede ser Kamra. Solo usa la
biblioteca estándar (Python ≥ 3.9) y se ejecuta como fichero, nunca como módulo del paquete:

    python3 -I vigia.py [--env FICHERO] [--una-vez | --bucle | --estado | --prueba-canales | --comprobar]

En el servidor corre en su propio contenedor con la imagen de Kamra (deploy/monitoring/compose.yml, `--bucle`); en la
máquina del hub, si se quiere un segundo ojo desde fuera del servidor, con cron (`--una-vez` cada minuto). Todo se
configura con variables de entorno HR_VIGIA_* (o un fichero KEY=VALUE con `--env`); la lista, en
deploy/monitoring/env.example.

Cada vuelta:
1. `GET /api/method/ping`. Sin «pong», la señal `caida`, que solo avisa tras HR_VIGIA_CAIDA_CONFIRMA vueltas seguidas
   (3: un despliegue o un reinicio no avisan) y tras HR_VIGIA_CAIDA_MIN minutos sin respuesta (2, HR-374); mientras Kamra no responde, las demás señales guardan su estado, y
   cuando vuelve, `programador` espera HR_VIGIA_GRACIA_MIN (10) antes de avisar: el programador de Frappe no late
   hasta su primer tick (4 min) tras arrancar.
2. `GET /api/method/kamra_uy.health.status` con la cabecera X-HR-Health-Token: el estado de cada señal de Kamra
   (programador, revisiones, ari, cfe, errores, disco, core; kamra_uy/health/checks.py) y su detalle.
3. El disco de las rutas de HR_VIGIA_DISCOS, visto desde aquí (`disco_servidor`).
4. Avisa: ALERTA cuando una señal pasa a roja, SIGUE cada HR_VIGIA_REPITE_MIN minutos mientras siga roja y RESUELTO
   cuando vuelve a verde; todo lo de una vuelta va en un solo mensaje, por todos los canales configurados. Si no lo
   entrega ninguno, la vuelta siguiente lo intenta otra vez. El estado vive en HR_VIGIA_ESTADO (JSON).
5. Si hay HR_VIGIA_LATIDO_URL (p. ej. Healthchecks.io), la llama: si el servidor entero se cae, el vigía calla y ese
   servicio avisa.

Lo que imprime (una línea por vuelta) nunca lleva el token, las claves ni la dirección de los webhooks.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

UTC = timezone.utc  # noqa: UP017 — sin datetime.UTC: el vigía corre también con Python 3.9 (AlmaLinux 9, el hub)

LABELS = {
	"caida": "Kamra no responde",
	"programador": "El programador de tareas de Kamra",
	"revisiones": "Reservas de Channex sin acusar",
	"ari": "Envíos a Channex rechazados",
	"cfe": "Facturas electrónicas (CFE) pendientes",
	"errores": "Errores de Kamra",
	"disco": "Disco de Kamra",
	"core": "El servicio de cobros y facturas (core)",
	"disco_servidor": "Disco del servidor",
}
KINDS = {"alerta": "ALERTA", "sigue": "SIGUE", "resuelto": "RESUELTO"}
# Tras una caída (un despliegue pasa por el 503 del mantenimiento), el programador de Frappe no late hasta su primer
# tick (4 min): estas señales esperan HR_VIGIA_GRACIA_MIN antes de avisar.
GRACE_SIGNALS = ("programador",)
PING = "/api/method/ping"
STATUS = "/api/method/kamra_uy.health.status"
DEFAULT_STATE = "~/.local/state/hotel-roosevelt/vigia.json"


class ConfigError(Exception):
	"""Falta o sobra algo en la configuración: el vigía no arranca."""


def _list(value) -> list:
	return [v.strip() for v in (value or "").split(",") if v.strip()]


def _int(env, key: str, default: int) -> int:
	raw = (env.get(key) or "").strip()
	if not raw:
		return default
	try:
		return int(raw)
	except ValueError:
		raise ConfigError(f"{key} tiene que ser un número entero (es «{raw}»)") from None


@dataclass
class Config:
	url: str
	token: str = ""
	host: str = ""
	basic_auth: str = ""
	nombre: str = "producción"
	estado: str = DEFAULT_STATE
	intervalo: int = 60
	caida_confirma: int = 3
	caida_min: int = 2
	repite_min: int = 240
	gracia_min: int = 10
	discos: list = field(default_factory=list)
	disco_pct: int = 90
	latido_url: str = ""
	timeout: int = 10
	slack_webhook: str = ""
	smtp_host: str = ""
	smtp_port: int = 587
	smtp_user: str = ""
	smtp_password: str = ""
	smtp_from: str = ""
	smtp_tls: str = "starttls"
	correo_a: list = field(default_factory=list)
	whatsapp_token: str = ""
	whatsapp_phone_id: str = ""
	whatsapp_a: list = field(default_factory=list)
	whatsapp_plantilla: str = ""
	whatsapp_idioma: str = "es"
	whatsapp_api: str = "https://graph.facebook.com/v21.0"

	def channels(self) -> list:
		out = []
		if self.slack_webhook:
			out.append("slack")
		if self.smtp_host:
			out.append("correo")
		if self.whatsapp_token:
			out.append("whatsapp")
		return out


def config_from_env(env) -> Config:
	"""La configuración desde las variables HR_VIGIA_* (ConfigError si falta algo o un canal va a medias)."""
	g = lambda key, default="": (env.get(key) or default).strip()  # noqa: E731
	url = g("HR_VIGIA_URL").rstrip("/")
	if not url.startswith(("http://", "https://")):
		raise ConfigError("HR_VIGIA_URL tiene que ser la dirección de Kamra, con http:// o https://")
	cfg = Config(
		url=url, token=g("HR_VIGIA_TOKEN"), host=g("HR_VIGIA_HOST"), basic_auth=g("HR_VIGIA_BASIC_AUTH"),
		nombre=g("HR_VIGIA_NOMBRE", "producción"), estado=os.path.expanduser(g("HR_VIGIA_ESTADO", DEFAULT_STATE)),
		intervalo=_int(env, "HR_VIGIA_INTERVALO", 60), caida_confirma=_int(env, "HR_VIGIA_CAIDA_CONFIRMA", 3),
		caida_min=_int(env, "HR_VIGIA_CAIDA_MIN", 2),
		repite_min=_int(env, "HR_VIGIA_REPITE_MIN", 240), gracia_min=_int(env, "HR_VIGIA_GRACIA_MIN", 10),
		discos=_list(env.get("HR_VIGIA_DISCOS")),
		disco_pct=_int(env, "HR_VIGIA_DISCO_PCT", 90), latido_url=g("HR_VIGIA_LATIDO_URL"),
		timeout=_int(env, "HR_VIGIA_TIMEOUT", 10), slack_webhook=g("HR_VIGIA_SLACK_WEBHOOK"),
		smtp_host=g("HR_VIGIA_SMTP_HOST"), smtp_port=_int(env, "HR_VIGIA_SMTP_PORT", 587),
		smtp_user=g("HR_VIGIA_SMTP_USER"), smtp_password=g("HR_VIGIA_SMTP_PASSWORD"),
		smtp_from=g("HR_VIGIA_SMTP_FROM") or g("HR_VIGIA_SMTP_USER"),
		smtp_tls=g("HR_VIGIA_SMTP_TLS", "ssl" if _int(env, "HR_VIGIA_SMTP_PORT", 587) == 465 else "starttls"),
		correo_a=_list(env.get("HR_VIGIA_CORREO_A")), whatsapp_token=g("HR_VIGIA_WHATSAPP_TOKEN"),
		whatsapp_phone_id=g("HR_VIGIA_WHATSAPP_PHONE_ID"), whatsapp_a=_list(env.get("HR_VIGIA_WHATSAPP_A")),
		whatsapp_plantilla=g("HR_VIGIA_WHATSAPP_PLANTILLA"), whatsapp_idioma=g("HR_VIGIA_WHATSAPP_IDIOMA", "es"),
		whatsapp_api=g("HR_VIGIA_WHATSAPP_API", "https://graph.facebook.com/v21.0").rstrip("/"))
	if cfg.smtp_host and not (cfg.smtp_from and cfg.correo_a):
		raise ConfigError("el correo necesita HR_VIGIA_SMTP_FROM (o HR_VIGIA_SMTP_USER) y HR_VIGIA_CORREO_A")
	if cfg.smtp_tls not in ("starttls", "ssl", "none"):
		raise ConfigError("HR_VIGIA_SMTP_TLS es starttls, ssl o none")
	if cfg.whatsapp_token and not (cfg.whatsapp_phone_id and cfg.whatsapp_a):
		raise ConfigError("WhatsApp necesita HR_VIGIA_WHATSAPP_PHONE_ID y HR_VIGIA_WHATSAPP_A")
	if cfg.basic_auth and ":" not in cfg.basic_auth:
		raise ConfigError("HR_VIGIA_BASIC_AUTH es usuario:contraseña")
	return cfg


def read_env_file(path) -> dict:
	"""Un fichero KEY=VALUE (con `export` delante o comillas, como lo deja un .env); ignora comentarios y basura."""
	out = {}
	for line in Path(path).read_text().splitlines():
		line = line.strip()
		if not line or line.startswith("#") or "=" not in line:
			continue
		key, _, value = line.removeprefix("export ").partition("=")
		value = value.strip()
		if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
			value = value[1:-1]
		out[key.strip()] = value
	return out


# ── red ─────────────────────────────────────────────────────────────────────────────────────────────────────────
class _NoRedirect(urllib.request.HTTPRedirectHandler):
	def redirect_request(self, *args, **kwargs):  # una redirección es una respuesta, no algo que seguir
		return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http(method: str, url: str, headers=None, body: bytes | None = None, timeout: float = 10) -> tuple:
	"""(código, cuerpo). Un 4xx/5xx es una respuesta; sin respuesta, OSError."""
	req = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": "hr-vigia", **(headers or {})})
	try:
		with _OPENER.open(req, timeout=timeout) as resp:
			return resp.status, resp.read(1_000_000)
	except urllib.error.HTTPError as exc:
		return exc.code, exc.read(1_000_000) if exc.fp else b""


def _kamra_headers(cfg: Config, with_token: bool) -> dict:
	headers = {"Accept": "application/json"}
	if cfg.host:
		headers["Host"] = cfg.host
	if cfg.basic_auth:
		headers["Authorization"] = "Basic " + base64.b64encode(cfg.basic_auth.encode()).decode()
	if with_token and cfg.token:
		headers["X-HR-Health-Token"] = cfg.token
	return headers


# ── observar ────────────────────────────────────────────────────────────────────────────────────────────────────
def _down(detail: str) -> dict:
	return {"caida": {"status": "fail", "detail": detail}}


def _kamra(cfg: Config, http) -> dict:
	try:
		code, body = http("GET", cfg.url + PING, headers=_kamra_headers(cfg, False), timeout=cfg.timeout)
	except OSError as exc:
		return _down(f"{cfg.url} no responde ({type(exc).__name__})")
	try:
		pong = json.loads(body).get("message") == "pong"
	except (ValueError, AttributeError):
		pong = False
	if code != 200 or not pong:
		return _down(f"el ping de Kamra no contesta «pong» (HTTP {code})")
	try:
		code, body = http("GET", cfg.url + STATUS, headers=_kamra_headers(cfg, True), timeout=cfg.timeout + 20)
		signals = json.loads(body)["message"]["signals"] if code == 200 else None
	except OSError as exc:
		return _down(f"el estado de Kamra no responde ({type(exc).__name__})")
	except (ValueError, KeyError, TypeError):
		signals = None
	if not isinstance(signals, dict):
		return _down(f"el estado de Kamra no contesta bien (HTTP {code})")
	out = {"caida": {"status": "ok", "detail": "responde"}}
	for name, value in signals.items():
		if isinstance(value, dict) and value.get("status") in ("ok", "fail", "off"):
			out[name] = {"status": value["status"], "detail": str(value.get("detail") or "")[:300]}
	return out


def _disks(cfg: Config, disk_usage) -> dict:
	if not cfg.discos:
		return {}
	full, seen = [], []
	for path in cfg.discos:
		try:
			total, used, free = disk_usage(path)
		except OSError as exc:
			full.append(f"{path}: no lo puedo medir ({type(exc).__name__})")
			continue
		pct = round(used * 100 / total) if total else 100
		seen.append(f"{path} {pct} %")
		if pct >= cfg.disco_pct:
			full.append(f"{path} usado {pct} % ({free / 2**30:.1f} GB libres)")
	if full:
		return {"disco_servidor": {"status": "fail", "detail": "; ".join(full)}}
	return {"disco_servidor": {"status": "ok", "detail": "; ".join(seen)}}


def observe(cfg: Config, http=http, disk_usage=shutil.disk_usage) -> dict:
	"""{señal: {"status": ok|fail|off, "detail"}} de esta vuelta. Con Kamra caída, solo `caida` (y el disco)."""
	return {**_kamra(cfg, http), **_disks(cfg, disk_usage)}


# ── decidir ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _iso(moment: datetime) -> str:
	return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value) -> datetime | None:
	try:
		return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
	except (TypeError, ValueError):
		return None


def decide(state: dict, seen: dict, now: datetime, cfg: Config) -> tuple:
	"""(estado nuevo, avisos). Un aviso: {"kind": alerta|sigue|resuelto, "signal", "detail", "since"}. Puro."""
	signals = {k: dict(v) for k, v in (state.get("signals") or {}).items()}
	notices = []
	recovered = state.get("recovered_at")
	if (seen.get("caida") or {}).get("status") == "ok" and (signals.get("caida") or {}).get("status") == "fail":
		recovered = _iso(now)  # Kamra acaba de volver
	back = _parse(recovered)
	grace_until = back + timedelta(minutes=cfg.gracia_min) if back else None
	for name, obs in seen.items():
		prev = signals.get(name) or {"status": "ok", "fails": 0, "alerted_at": None, "since": _iso(now)}
		entry = {"status": obs["status"], "detail": obs.get("detail", ""), "alerted_at": prev.get("alerted_at"),
		         "since": prev.get("since") if prev.get("status") == obs["status"] else _iso(now)}
		if obs["status"] == "fail":
			entry["fails"] = (prev.get("fails") or 0) + 1 if prev.get("status") == "fail" else 1
			alerted = _parse(prev.get("alerted_at"))
			need = cfg.caida_confirma if name == "caida" else 1
			waiting = name in GRACE_SIGNALS and grace_until is not None and now < grace_until
			# HR-374 (el fundador, 26-11: «debería ser 2 minutos»): la caída no avisa hasta llevar HR_VIGIA_CAIDA_MIN
			# minutos sin respuesta, contados desde la primera vuelta que la vio caída, sea cual sea el intervalo
			red_since = _parse(entry["since"]) or now
			long_enough = name != "caida" or now - red_since >= timedelta(minutes=cfg.caida_min)
			if alerted is None and entry["fails"] >= need and long_enough and not waiting:
				notices.append({"kind": "alerta", "signal": name, "detail": entry["detail"], "since": entry["since"]})
				entry["alerted_at"] = _iso(now)
			elif alerted is not None and now - alerted >= timedelta(minutes=cfg.repite_min):
				notices.append({"kind": "sigue", "signal": name, "detail": entry["detail"], "since": entry["since"]})
				entry["alerted_at"] = _iso(now)
		else:
			entry["fails"] = 0
			if prev.get("alerted_at"):
				notices.append({"kind": "resuelto", "signal": name, "detail": entry["detail"], "since": entry["since"]})
			entry["alerted_at"] = None
		signals[name] = entry
	return {"signals": signals, "last_run": _iso(now), "recovered_at": recovered}, notices


def _ago(since: str | None, now: datetime) -> str:
	start = _parse(since)
	if not start:
		return ""
	minutes = int((now - start).total_seconds() // 60)
	return f" (desde hace {minutes // 60} h {minutes % 60} min)" if minutes >= 60 else ""


def compose(notices: list, cfg: Config, now: datetime) -> tuple:
	"""(asunto, texto) de un mensaje con todos los avisos de la vuelta."""
	lines = []
	for n in notices:
		label = LABELS.get(n["signal"], n["signal"])
		tail = "" if n["kind"] == "resuelto" else (f": {n['detail']}" if n["detail"] else "")
		lines.append(f"{KINDS[n['kind']]} · {label}{tail}{_ago(n['since'], now) if n['kind'] == 'sigue' else ''}")
	worst = "ALERTA" if any(n["kind"] != "resuelto" for n in notices) else "Resuelto"
	labels = ", ".join(dict.fromkeys(LABELS.get(n["signal"], n["signal"]) for n in notices))
	subject = f"[Hotel Roosevelt · {cfg.nombre}] {worst}: {labels}"
	return subject, f"Hotel Roosevelt · {cfg.nombre}\n" + "\n".join(lines)


# ── entregar ────────────────────────────────────────────────────────────────────────────────────────────────────
def _post_json(http, url: str, payload: dict, timeout: int, headers=None) -> None:
	code, _body = http("POST", url, headers={"Content-Type": "application/json", **(headers or {})},
	                  body=json.dumps(payload).encode(), timeout=timeout)
	if not 200 <= code < 300:
		raise OSError(f"HTTP {code}")


def _slack(cfg, subject, text, http, smtp) -> None:
	_post_json(http, cfg.slack_webhook, {"text": text}, cfg.timeout)


def _correo(cfg, subject, text, http, smtp) -> None:
	msg = EmailMessage()
	msg["Subject"], msg["From"], msg["To"] = subject, cfg.smtp_from, ", ".join(cfg.correo_a)
	msg.set_content(text)
	context = ssl.create_default_context()
	extra = {"use_ssl": True, "context": context} if cfg.smtp_tls == "ssl" else {}
	with smtp(cfg.smtp_host, cfg.smtp_port, timeout=cfg.timeout, **extra) as server:
		if cfg.smtp_tls == "starttls":
			server.starttls(context=context)
		if cfg.smtp_user:
			server.login(cfg.smtp_user, cfg.smtp_password)
		server.send_message(msg)


def _whatsapp(cfg, subject, text, http, smtp) -> None:
	one_line = " · ".join(line for line in text.splitlines() if line.strip())[:1000]
	for to in cfg.whatsapp_a:
		if cfg.whatsapp_plantilla:
			payload = {"messaging_product": "whatsapp", "to": to, "type": "template", "template": {
				"name": cfg.whatsapp_plantilla, "language": {"code": cfg.whatsapp_idioma},
				"components": [{"type": "body", "parameters": [{"type": "text", "text": one_line}]}]}}
		else:  # solo vale dentro de la ventana de 24 h de WhatsApp (ver deploy/monitoring/README.md)
			payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4000]}}
		_post_json(http, f"{cfg.whatsapp_api}/{cfg.whatsapp_phone_id}/messages", payload, cfg.timeout,
		           headers={"Authorization": f"Bearer {cfg.whatsapp_token}"})


SENDERS = {"slack": _slack, "correo": _correo, "whatsapp": _whatsapp}


def smtp_connect(host: str, port: int, timeout: float, use_ssl: bool = False, context=None):
	"""La conexión SMTP de verdad (los tests inyectan otra con la misma firma)."""
	if use_ssl:
		return smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
	return smtplib.SMTP(host, port, timeout=timeout)


def deliver(cfg: Config, subject: str, text: str, http=http, smtp=smtp_connect) -> dict:
	"""{canal: "ok" | "error: <tipo y motivo, sin secretos>"} de cada canal configurado."""
	results = {}
	for name in cfg.channels():
		try:
			SENDERS[name](cfg, subject, text, http, smtp)
			results[name] = "ok"
		except Exception as exc:  # un canal que falla no tapa a los otros
			results[name] = f"error: {type(exc).__name__}"
	return results


# ── una vuelta ──────────────────────────────────────────────────────────────────────────────────────────────────
def _load_state(path: str) -> dict:
	try:
		return json.loads(Path(path).read_text())
	except (OSError, ValueError):
		return {}


def _save_state(path: str, state: dict) -> None:
	target = Path(path)
	target.parent.mkdir(parents=True, exist_ok=True)
	tmp = target.with_suffix(".tmp")
	tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
	os.replace(tmp, target)


def run_once(cfg: Config, now: datetime | None = None, http=http, smtp=smtp_connect, disk_usage=shutil.disk_usage,
             out=print) -> dict:
	"""Una vuelta: observar, decidir, avisar, guardar el estado y llamar al latido. Devuelve lo observado."""
	now = now or datetime.now(UTC)
	state = _load_state(cfg.estado)
	seen = observe(cfg, http=http, disk_usage=disk_usage)
	new_state, notices = decide(state, seen, now, cfg)
	sent = ""
	if notices:
		subject, text = compose(notices, cfg, now)
		results = deliver(cfg, subject, text, http=http, smtp=smtp)
		sent = " · avisos: " + ", ".join(f"{n['kind']} {n['signal']}" for n in notices)
		sent += f" ({', '.join(f'{k} {v}' for k, v in results.items()) or 'sin canales: solo este registro'})"
		if results and "ok" not in results.values():
			# nadie lo entregó: esas señales vuelven a como estaban, y la próxima vuelta lo intenta otra vez
			for n in notices:
				previous = (state.get("signals") or {}).get(n["signal"])
				if previous is None:
					new_state["signals"].pop(n["signal"], None)
				else:
					new_state["signals"][n["signal"]] = previous
	_save_state(cfg.estado, new_state)
	failing = [k for k, v in seen.items() if v["status"] == "fail"]
	out(f"{_iso(now)} {'fail: ' + ', '.join(failing) if failing else 'ok'}{sent}")
	if cfg.latido_url:
		try:
			http("GET", cfg.latido_url, timeout=cfg.timeout)
		except OSError as exc:
			out(f"{_iso(now)} el latido externo no responde ({type(exc).__name__})")
	return seen


def probar_canales(cfg: Config, http=http, smtp=smtp_connect) -> dict:
	"""Un mensaje de prueba por cada canal (--prueba-canales)."""
	text = (f"Hotel Roosevelt · {cfg.nombre}\nPrueba de avisos del vigía: si lees esto, este canal funciona. "
	        "No hay nada que hacer.")
	return deliver(cfg, f"[Hotel Roosevelt · {cfg.nombre}] Prueba de avisos", text, http=http, smtp=smtp)


def main(argv=None) -> int:
	parser = argparse.ArgumentParser(description="El vigía de Kamra (HR-21).")
	parser.add_argument("--env", help="fichero KEY=VALUE con las variables HR_VIGIA_* (manda sobre el entorno)")
	mode = parser.add_mutually_exclusive_group()
	mode.add_argument("--una-vez", action="store_true", help="una vuelta y sale (por defecto)")
	mode.add_argument("--bucle", action="store_true", help="una vuelta cada HR_VIGIA_INTERVALO segundos")
	mode.add_argument("--estado", action="store_true", help="imprime lo que ve, sin avisar ni guardar nada")
	mode.add_argument("--prueba-canales", action="store_true", help="manda un mensaje de prueba por cada canal")
	mode.add_argument("--comprobar", action="store_true", help="solo lee la configuración y dice qué canales hay")
	args = parser.parse_args(argv)
	env = dict(os.environ)
	try:
		if args.env:
			env.update(read_env_file(args.env))
		cfg = config_from_env(env)
	except (ConfigError, OSError) as exc:
		print(f"vigía: {exc}", file=sys.stderr)
		return 2
	if not cfg.channels():
		print("vigía: sin canales configurados: los avisos solo salen en este registro", file=sys.stderr)
	if not cfg.token:
		print("vigía: sin HR_VIGIA_TOKEN: Kamra solo enseña el estado de cada señal, sin el detalle", file=sys.stderr)
	if args.comprobar:
		print(f"vigía: {cfg.url} ({cfg.nombre}) · canales: {', '.join(cfg.channels()) or 'ninguno'} · "
		      f"cada {cfg.intervalo} s · caída tras {cfg.caida_confirma} vueltas · caída tras {cfg.caida_min} min · "
		      f"repite cada {cfg.repite_min} min")
		return 0
	if args.estado:
		print(json.dumps(observe(cfg), ensure_ascii=False, indent=1))
		return 0
	if args.prueba_canales:
		results = probar_canales(cfg)
		print(json.dumps(results, ensure_ascii=False))
		return 0 if results and all(v == "ok" for v in results.values()) else 1
	if not args.bucle:
		run_once(cfg, out=lambda line: print(line, flush=True))
		return 0
	while True:  # en el contenedor: una vuelta que falla no para el vigía
		started = time.monotonic()
		try:
			run_once(cfg, out=lambda line: print(line, flush=True))
		except Exception as exc:
			print(f"{_iso(datetime.now(UTC))} la vuelta falló: {type(exc).__name__}", flush=True)
		time.sleep(max(1.0, cfg.intervalo - (time.monotonic() - started)))


if __name__ == "__main__":
	sys.exit(main())
