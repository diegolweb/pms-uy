"""El vigía (HR-21): mira Kamra desde fuera cada minuto y avisa al fundador (correo o WhatsApp) y al Slack del hub
cuando una señal se pone roja, mientras siga roja (cada REPITE_MIN) y cuando se arregla. Sin frappe ni red: el HTTP, el
SMTP y el disco van inyectados. Corre en `bench run-tests` y con `python3 -m unittest` desde esta carpeta."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar

try:
	from kamra_uy.health.tests._local import UnitTestCase, load
except ImportError:  # fuera del bench
	from _local import UnitTestCase, load

vigia = load("vigia")

URL = "https://reservas.example.com"
T0 = datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc)  # noqa: UP017 — el vigía y su prueba, también con Python 3.9
GREEN = {"programador": "último latido hace 20 s", "revisiones": "ninguna sin acusar", "ari": "sin rechazos",
         "cfe": "ninguno pendiente", "errores": "0 en la última hora", "disco": "usado 40 %", "core": "responde"}


def _status_body(**overrides) -> bytes:
	signals = {name: {"status": "ok", "detail": detail} for name, detail in GREEN.items()}
	for name, value in overrides.items():
		signals[name] = value
	failing = any(s["status"] == "fail" for s in signals.values())
	return json.dumps({"message": {"status": "fail" if failing else "ok", "checked_at": "x",
	                               "signals": signals}}).encode()


class FakeNet:
	"""El HTTP del vigía: Kamra (ping y estado), Slack, WhatsApp y el latido externo."""

	def __init__(self):
		self.calls = []
		self.ping = (200, b'{"message":"pong"}')
		self.status = (200, _status_body())
		self.down = set()  # urls que no responden

	def set_signal(self, name, status, detail):
		self.status = (200, _status_body(**{name: {"status": status, "detail": detail}}))

	def __call__(self, method, url, headers=None, body=None, timeout=None):
		self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
		                   "body": json.loads(body) if body else None})
		if any(url.startswith(d) for d in self.down):
			self.calls[-1]["failed"] = True
			raise OSError("sin respuesta")
		if url.endswith("/api/method/ping"):
			return self.ping
		if url.endswith("/api/method/kamra_uy.health.status"):
			return self.status
		return (200, b"{}")

	def posts(self, prefix):
		"""Los POST que llegaron (no los que fueron a una dirección caída)."""
		return [c for c in self.calls if c["method"] == "POST" and c["url"].startswith(prefix) and not c.get("failed")]


class FakeSMTP:
	sent: ClassVar[list] = []
	fail = False

	def __init__(self, host, port, timeout=None, use_ssl=False, context=None):
		self.host, self.port, self.tls, self.login_as = host, port, use_ssl, None

	def __enter__(self):
		return self

	def __exit__(self, *a):
		return False

	def starttls(self, context=None):
		self.tls = True

	def login(self, user, password):
		self.login_as = user

	def send_message(self, msg):
		if FakeSMTP.fail:
			raise OSError("smtp caído")
		FakeSMTP.sent.append({"host": self.host, "tls": self.tls, "login": self.login_as, "to": msg["To"],
		                      "from": msg["From"], "subject": msg["Subject"], "body": msg.get_content()})


def _env(tmp: str, **extra) -> dict:
	env = {"HR_VIGIA_URL": URL, "HR_VIGIA_TOKEN": "token-de-prueba", "HR_VIGIA_NOMBRE": "producción",
	       "HR_VIGIA_ESTADO": os.path.join(tmp, "estado.json"),
	       "HR_VIGIA_SLACK_WEBHOOK": "https://hooks.slack.example.com/services/T0/B0/x",
	       "HR_VIGIA_SMTP_HOST": "smtp.example.com", "HR_VIGIA_SMTP_USER": "avisos@example.com",
	       "HR_VIGIA_SMTP_PASSWORD": "clave-de-prueba", "HR_VIGIA_SMTP_FROM": "avisos@example.com",
	       "HR_VIGIA_CORREO_A": "fundador@example.com, otro@example.com",
	       "HR_VIGIA_WHATSAPP_TOKEN": "wa-de-prueba", "HR_VIGIA_WHATSAPP_PHONE_ID": "123",
	       "HR_VIGIA_WHATSAPP_A": "59899000000", "HR_VIGIA_WHATSAPP_PLANTILLA": "alerta_sistema",
	       "HR_VIGIA_WHATSAPP_API": "https://graph.example.com/v21.0"}
	env.update(extra)
	return env


class VigiaCase(UnitTestCase):
	def setUp(self):
		super().setUp()
		self.tmp = tempfile.mkdtemp(prefix="hr21-vigia-")
		self.net = FakeNet()
		FakeSMTP.sent, FakeSMTP.fail = [], False
		self.cfg = vigia.config_from_env(_env(self.tmp))
		self.disk = lambda path: (100, 40, 60)
		self.printed = []

	def run_at(self, minutes: float, cfg=None) -> dict:
		return vigia.run_once(cfg or self.cfg, now=T0 + timedelta(minutes=minutes), http=self.net,
		                      smtp=FakeSMTP, disk_usage=self.disk, out=self.printed.append)

	def slack_texts(self) -> list[str]:
		return [c["body"]["text"] for c in self.net.posts("https://hooks.slack.example.com")]


class ConfigTestCase(VigiaCase):
	def test_the_url_is_required(self):
		with self.assertRaises(vigia.ConfigError):
			vigia.config_from_env({})
		with self.assertRaises(vigia.ConfigError):
			vigia.config_from_env({"HR_VIGIA_URL": "ftp://x"})

	def test_defaults(self):
		cfg = vigia.config_from_env({"HR_VIGIA_URL": URL + "/"})
		self.assertEqual(cfg.url, URL)
		self.assertEqual((cfg.intervalo, cfg.caida_confirma, cfg.repite_min, cfg.disco_pct, cfg.gracia_min),
		                 (60, 3, 240, 90, 10))
		self.assertEqual(cfg.channels(), [])

	def test_the_channels_come_from_their_variables(self):
		self.assertEqual(self.cfg.channels(), ["slack", "correo", "whatsapp"])
		self.assertEqual(self.cfg.correo_a, ["fundador@example.com", "otro@example.com"])

	def test_a_channel_with_a_missing_variable_is_an_error(self):
		with self.assertRaises(vigia.ConfigError):
			vigia.config_from_env({"HR_VIGIA_URL": URL, "HR_VIGIA_SMTP_HOST": "smtp.example.com"})
		with self.assertRaises(vigia.ConfigError):
			vigia.config_from_env({"HR_VIGIA_URL": URL, "HR_VIGIA_WHATSAPP_TOKEN": "x"})

	def test_the_env_file(self):
		path = Path(self.tmp) / "vigia.env"
		path.write_text("# comentario\nexport HR_VIGIA_URL='https://dev.example.com'\nHR_VIGIA_NOMBRE=\"dev\"\n\n"
		                "HR_VIGIA_INTERVALO=120\nROTA\n")
		values = vigia.read_env_file(path)
		self.assertEqual(values, {"HR_VIGIA_URL": "https://dev.example.com", "HR_VIGIA_NOMBRE": "dev",
		                          "HR_VIGIA_INTERVALO": "120"})


class ObserveTestCase(VigiaCase):
	def test_all_green(self):
		seen = vigia.observe(self.cfg, http=self.net, disk_usage=self.disk)
		self.assertEqual(seen["caida"]["status"], "ok")
		self.assertEqual({k: v["status"] for k, v in seen.items() if k != "caida"}, dict.fromkeys(GREEN, "ok"))

	def test_the_token_host_and_basic_auth_go_in_the_request(self):
		cfg = vigia.config_from_env(_env(self.tmp, HR_VIGIA_HOST="reservas.example.com",
		                                 HR_VIGIA_BASIC_AUTH="dev:secreto"))
		vigia.observe(cfg, http=self.net, disk_usage=self.disk)
		status_call = next(c for c in self.net.calls if c["url"].endswith("kamra_uy.health.status"))
		self.assertEqual(status_call["headers"]["X-HR-Health-Token"], "token-de-prueba")
		self.assertEqual(status_call["headers"]["Host"], "reservas.example.com")
		self.assertTrue(status_call["headers"]["Authorization"].startswith("Basic "))
		ping_call = next(c for c in self.net.calls if c["url"].endswith("/ping"))
		self.assertNotIn("X-HR-Health-Token", ping_call["headers"])

	def test_no_pong_is_down_and_nothing_else_is_read(self):
		self.net.ping = (502, b"Bad Gateway")
		seen = vigia.observe(self.cfg, http=self.net, disk_usage=self.disk)
		self.assertEqual(list(seen), ["caida"])
		self.assertEqual(seen["caida"]["status"], "fail")
		self.assertIn("502", seen["caida"]["detail"])

	def test_no_answer_is_down(self):
		self.net.down.add(URL)
		seen = vigia.observe(self.cfg, http=self.net, disk_usage=self.disk)
		self.assertEqual(seen["caida"]["status"], "fail")
		self.assertIn("no responde", seen["caida"]["detail"])

	def test_a_broken_status_endpoint_is_down(self):
		self.net.status = (500, b"<html>")
		seen = vigia.observe(self.cfg, http=self.net, disk_usage=self.disk)
		self.assertEqual(seen["caida"]["status"], "fail")
		self.assertIn("estado", seen["caida"]["detail"])

	def test_the_server_disks(self):
		cfg = vigia.config_from_env(_env(self.tmp, HR_VIGIA_DISCOS="/estado, /otro"))
		usage = {"/estado": (100, 95, 5), "/otro": (100, 10, 90)}
		seen = vigia.observe(cfg, http=self.net, disk_usage=lambda p: usage[p])
		self.assertEqual(seen["disco_servidor"]["status"], "fail")
		self.assertIn("/estado", seen["disco_servidor"]["detail"])
		self.assertNotIn("/otro", seen["disco_servidor"]["detail"])
		self.net.ping = (502, b"")
		self.assertEqual(set(vigia.observe(cfg, http=self.net, disk_usage=lambda p: usage[p])),
		                 {"caida", "disco_servidor"})


class DecideTestCase(VigiaCase):
	def test_a_red_signal_alerts_once_then_repeats_then_resolves(self):
		self.net.set_signal("programador", "fail", "el último latido fue hace 6 min")
		self.run_at(0)
		self.assertEqual(len(self.slack_texts()), 1)
		self.assertIn("ALERTA", self.slack_texts()[0])
		self.assertIn("programador", self.slack_texts()[0].lower())
		self.assertIn("hace 6 min", self.slack_texts()[0])
		self.run_at(1)
		self.run_at(239)
		self.assertEqual(len(self.slack_texts()), 1)
		self.run_at(240)
		self.assertEqual(len(self.slack_texts()), 2)
		self.assertIn("SIGUE", self.slack_texts()[1])
		self.net.set_signal("programador", "ok", "último latido hace 10 s")
		self.run_at(241)
		self.assertEqual(len(self.slack_texts()), 3)
		self.assertIn("RESUELTO", self.slack_texts()[2])
		self.run_at(242)
		self.assertEqual(len(self.slack_texts()), 3)

	def test_down_needs_three_runs_in_a_row(self):
		self.net.ping = (503, b"")
		self.run_at(0)
		self.run_at(1)
		self.assertEqual(self.slack_texts(), [])
		self.run_at(2)
		self.assertEqual(len(self.slack_texts()), 1)
		self.assertIn("Kamra no responde", self.slack_texts()[0])

	def test_a_blip_does_not_alert(self):
		self.net.ping = (503, b"")
		self.run_at(0)
		self.net.ping = (200, b'{"message":"pong"}')
		self.run_at(1)
		self.net.ping = (503, b"")
		self.run_at(2)
		self.run_at(3)
		self.assertEqual(self.slack_texts(), [])

	def test_while_down_the_other_signals_keep_their_state(self):
		self.net.set_signal("revisiones", "fail", "2 sin acusar")
		self.run_at(0)
		self.net.ping = (503, b"")
		for minute in (1, 2, 3):
			self.run_at(minute)
		self.assertEqual(len(self.slack_texts()), 2)  # revisiones y luego la caída; ningún «resuelto» de revisiones
		self.assertNotIn("RESUELTO", " ".join(self.slack_texts()))

	def test_after_an_outage_the_scheduler_gets_a_grace_period(self):
		# un despliegue: mantenimiento (503), y el programador no late hasta su primer tick (4 min) tras arrancar
		self.net.ping = (503, b"")
		self.run_at(0)
		self.net.ping = (200, b'{"message":"pong"}')
		self.net.set_signal("programador", "fail", "el último latido fue hace 8 min (más de 7 min)")
		self.run_at(1)
		self.run_at(10)
		self.assertEqual(self.slack_texts(), [])
		self.run_at(11)  # pasado el margen (10 min desde que volvió) y aún rojo: ahora sí
		self.assertEqual(len(self.slack_texts()), 1)
		self.assertIn("El programador", self.slack_texts()[0])

	def test_the_grace_period_only_covers_the_scheduler(self):
		self.net.ping = (503, b"")
		self.run_at(0)
		self.net.ping = (200, b'{"message":"pong"}')
		self.net.set_signal("cfe", "fail", "1 a revisar")
		self.run_at(1)
		self.assertEqual(len(self.slack_texts()), 1)

	def test_without_an_outage_the_scheduler_alerts_at_once(self):
		self.net.set_signal("programador", "fail", "hace 8 min")
		self.run_at(0)
		self.assertEqual(len(self.slack_texts()), 1)

	def test_off_is_not_an_alert(self):
		self.net.set_signal("core", "off", "sin hr_core_base_url")
		self.run_at(0)
		self.assertEqual(self.slack_texts(), [])

	def test_several_changes_go_in_one_message(self):
		self.net.status = (200, _status_body(revisiones={"status": "fail", "detail": "1 sin acusar"},
		                                     cfe={"status": "fail", "detail": "2 pendientes"}))
		self.run_at(0)
		self.assertEqual(len(self.slack_texts()), 1)
		self.assertIn("Channex", self.slack_texts()[0])
		self.assertIn("CFE", self.slack_texts()[0])


class DeliverTestCase(VigiaCase):
	def test_every_channel_gets_the_alert(self):
		self.net.set_signal("cfe", "fail", "3 en cola en core desde hace más de 30 min")
		self.run_at(0)
		self.assertEqual(len(self.slack_texts()), 1)
		self.assertEqual(len(FakeSMTP.sent), 1)
		mail = FakeSMTP.sent[0]
		self.assertEqual(mail["to"], "fundador@example.com, otro@example.com")
		self.assertTrue(mail["tls"])
		self.assertEqual(mail["login"], "avisos@example.com")
		self.assertIn("ALERTA", mail["subject"])
		self.assertIn("producción", mail["subject"])
		self.assertIn("3 en cola", mail["body"])
		wa = self.net.posts("https://graph.example.com/v21.0/123/messages")
		self.assertEqual(len(wa), 1)
		self.assertEqual(wa[0]["headers"]["Authorization"], "Bearer wa-de-prueba")
		payload = wa[0]["body"]
		self.assertEqual((payload["to"], payload["type"]), ("59899000000", "template"))
		self.assertEqual(payload["template"]["name"], "alerta_sistema")
		param = payload["template"]["components"][0]["parameters"][0]["text"]
		self.assertIn("3 en cola", param)
		self.assertNotIn("\n", param)

	def test_port_465_is_smtp_over_ssl(self):
		cfg = vigia.config_from_env(_env(self.tmp, HR_VIGIA_SMTP_PORT="465"))
		self.assertEqual(cfg.smtp_tls, "ssl")
		self.net.set_signal("cfe", "fail", "x")
		self.run_at(0, cfg)
		self.assertTrue(FakeSMTP.sent[0]["tls"])

	def test_whatsapp_without_a_template_sends_text(self):
		cfg = vigia.config_from_env(_env(self.tmp, HR_VIGIA_WHATSAPP_PLANTILLA=""))
		self.net.set_signal("cfe", "fail", "x")
		self.run_at(0, cfg)
		payload = self.net.posts("https://graph.example.com")[0]["body"]
		self.assertEqual(payload["type"], "text")
		self.assertIn("ALERTA", payload["text"]["body"])

	def test_if_no_channel_delivers_it_tries_again_next_run(self):
		self.net.down.update({"https://hooks.slack.example.com", "https://graph.example.com"})
		FakeSMTP.fail = True
		self.net.set_signal("disco", "fail", "usado 93 %")
		self.run_at(0)
		self.net.down.clear()
		FakeSMTP.fail = False
		self.run_at(1)
		self.assertEqual(len(self.slack_texts()), 1)
		self.assertIn("ALERTA", self.slack_texts()[0])
		self.run_at(2)
		self.assertEqual(len(self.slack_texts()), 1)

	def test_one_channel_is_enough(self):
		self.net.down.add("https://graph.example.com")
		FakeSMTP.fail = True
		self.net.set_signal("disco", "fail", "usado 93 %")
		self.run_at(0)
		self.run_at(1)
		self.assertEqual(len(self.slack_texts()), 1)

	def test_the_state_survives_a_restart(self):
		self.net.set_signal("errores", "fail", "12 en la última hora")
		self.run_at(0)
		saved = json.loads(Path(self.cfg.estado).read_text())
		self.assertEqual(saved["signals"]["errores"]["status"], "fail")
		cfg = vigia.config_from_env(_env(self.tmp))  # otro proceso, el mismo fichero
		self.run_at(1, cfg)
		self.assertEqual(len(self.slack_texts()), 1)

	def test_the_dead_man_switch_is_pinged_every_run(self):
		cfg = vigia.config_from_env(_env(self.tmp, HR_VIGIA_LATIDO_URL="https://hc.example.com/ping/abc"))
		self.run_at(0, cfg)
		self.run_at(1, cfg)
		self.assertEqual(len([c for c in self.net.calls if c["url"] == "https://hc.example.com/ping/abc"]), 2)

	def test_channel_test_message(self):
		results = vigia.probar_canales(self.cfg, http=self.net, smtp=FakeSMTP)
		self.assertEqual(results, {"slack": "ok", "correo": "ok", "whatsapp": "ok"})
		self.assertIn("prueba", self.slack_texts()[0].lower())
		FakeSMTP.fail = True
		self.assertNotEqual(vigia.probar_canales(self.cfg, http=self.net, smtp=FakeSMTP)["correo"], "ok")

	def test_no_secret_in_what_it_prints(self):
		lines = []
		self.net.down.add("https://hooks.slack.example.com")
		self.net.set_signal("disco", "fail", "usado 93 %")
		vigia.run_once(self.cfg, now=T0, http=self.net, smtp=FakeSMTP, disk_usage=self.disk, out=lines.append)
		printed = "\n".join(lines)
		self.assertIn("disco", printed)
		for secret in ("token-de-prueba", "clave-de-prueba", "wa-de-prueba", "/services/T0/B0/x"):
			self.assertNotIn(secret, printed)


class MainTestCase(VigiaCase):
	def env_file(self, **extra) -> str:
		path = Path(self.tmp) / "vigia.env"
		path.write_text("\n".join(f"{k}={v}" for k, v in _env(self.tmp, **extra).items()) + "\n")
		return str(path)

	def test_check_mode_reads_the_file_and_names_the_channels_without_secrets(self):
		import contextlib
		import io

		out = io.StringIO()
		with contextlib.redirect_stdout(out):
			code = vigia.main(["--env", self.env_file(), "--comprobar"])
		self.assertEqual(code, 0)
		self.assertIn("slack, correo, whatsapp", out.getvalue())
		self.assertIn(URL, out.getvalue())
		for secret in ("token-de-prueba", "clave-de-prueba", "wa-de-prueba", "/services/T0/B0/x"):
			self.assertNotIn(secret, out.getvalue())

	def test_a_bad_file_is_exit_2(self):
		import contextlib
		import io

		err = io.StringIO()
		with contextlib.redirect_stderr(err):
			code = vigia.main(["--env", self.env_file(HR_VIGIA_URL="sin-esquema"), "--comprobar"])
		self.assertEqual(code, 2)
		self.assertIn("HR_VIGIA_URL", err.getvalue())
		with contextlib.redirect_stderr(err):
			self.assertEqual(vigia.main(["--env", str(Path(self.tmp) / "no-existe.env"), "--comprobar"]), 2)

	def test_check_mode_without_a_token_warns(self):
		import contextlib
		import io

		out = io.StringIO()
		with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
			vigia.main(["--env", self.env_file(HR_VIGIA_TOKEN=""), "--comprobar"])
		self.assertIn("sin HR_VIGIA_TOKEN", out.getvalue())
