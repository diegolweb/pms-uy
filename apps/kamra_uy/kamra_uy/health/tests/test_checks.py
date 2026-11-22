"""Las señales de salud de Kamra (HR-21) contra Kamra de verdad y un core simulado: cada una en verde y en rojo, el
endpoint /api/method/kamra_uy.health.status (lo público y lo que ve quien trae el token) y el latido del programador.

Lo corre `bench run-tests --app kamra_uy` (o `--module kamra_uy.health.tests.test_checks`). Cada test vacía, dentro de
su savepoint (se deshace al acabar), las filas de los demás que podrían contar: conexiones de Channex, revisiones,
outbox de ARI, UY CFE y Error Log.
"""

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime
from kamra.tests.audit_fixtures import AuditTestCase

from kamra_uy import health
from kamra_uy.fiscal import connector
from kamra_uy.health import beat, checks
from kamra_uy.payments.tests.fixtures import CODE, PROP, gateway_settings, make_uy_property

CORE = "http://core.test"
TOKEN = "token-de-salud-de-prueba"


class _Resp:
	def __init__(self, status: int, body):
		self.status_code = status
		self.content = json.dumps(body).encode()

	def json(self):
		return json.loads(self.content)


class FakeCore:
	"""Lo que contesta core a la salud: /health y listCfe (status=queued|contingency)."""

	def __init__(self):
		self.calls, self.down, self.health = [], False, (200, {"status": "ok", "db": "ok", "queue": "ok", "version": "t"})
		self.cfes = {"queued": [], "contingency": []}
		self.list_status = 200

	def request(self, method, url, data=None, headers=None, timeout=None):
		self.calls.append((method, url, headers or {}))
		if self.down:
			raise OSError("core is down")
		path = url[len(CORE):]
		if path == "/health":
			return _Resp(*self.health)
		if path.startswith("/v1/fiscal/cfe?"):
			if self.list_status != 200:
				return _Resp(self.list_status, {"code": "forbidden", "status": self.list_status, "title": "x",
				                                "type": "about:blank", "detail": "la clave no tiene el alcance read"})
			status = path.split("status=")[1].split("&")[0]
			return _Resp(200, {"items": self.cfes[status], "next_cursor": None})
		return _Resp(404, {"code": "not_found", "status": 404, "title": "x", "type": "about:blank"})


def _cfe(status: str, minutes_ago: int, **extra) -> dict:
	# core habla en UTC (Instant): la hora del sistema del sitio puede ser otra
	at = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
	return {"cfe_id": frappe.generate_hash(length=8), "status": status, "created_at": at, "updated_at": at, **extra}


class HealthCase(AuditTestCase):
	properties = (PROP,)

	def setUp(self):
		super().setUp()
		for doctype in ("Channel ARI Outbox", "Channel Booking Revision", "UY CFE", "Error Log"):
			frappe.db.delete(doctype)
		frappe.db.set_value("Channel Manager Connection", {"active": 1}, "active", 0)
		frappe.cache.delete_value(checks.CACHE_KEY)
		self.now = now_datetime()
		self.conf = self._stack.enter_context(patch.dict(frappe.local.conf, {"hr_core_base_url": CORE}))
		self.core = FakeCore()
		self._stack.enter_context(patch.object(connector, "_session", return_value=self.core))
		self.real_tick = checks._scheduler_tick
		self.inactive = self._stack.enter_context(patch.object(checks, "_scheduler_inactive", return_value=False))
		self.tick = self._stack.enter_context(patch.object(checks, "_scheduler_tick", return_value=240))
		self.disk = self._stack.enter_context(patch.object(checks, "_disk_usage", return_value=(100, 40, 60)))
		self.site_dir = tempfile.mkdtemp(prefix="hr21-latido-")
		self.real_beat_path = beat.path
		self._stack.enter_context(patch.object(beat, "path", return_value=os.path.join(self.site_dir, beat.FILE)))
		beat.heartbeat()

	def tearDown(self):
		frappe.cache.delete_value(checks.CACHE_KEY)
		super().tearDown()

	def signal(self, name: str) -> dict:
		return checks.report(self.now)["signals"][name]

	def connection(self, **fields) -> str:
		make_uy_property()
		return frappe.get_doc({"doctype": "Channel Manager Connection", "property": PROP, "provider": "Channex",
		                       "active": 1, "external_property_id": "cx-salud", "webhook_secret": "ut-hook",
		                       **fields}).insert(ignore_permissions=True).name

	def backdate(self, doctype: str, name: str, minutes: int, field: str = "creation") -> None:
		frappe.db.set_value(doctype, name, field, add_to_date(self.now, minutes=-minutes), update_modified=False)


class SchedulerTestCase(HealthCase):
	def test_a_fresh_beat_is_green(self):
		got = self.signal("programador")
		self.assertEqual(got["status"], "ok", got)

	def test_a_beat_within_the_frappe_tick_is_green(self):
		# Frappe 16 encola los cron cada 4 min (DEFAULT_SCHEDULER_TICK): un latido de hace 6 min es normal
		beat._write(add_to_date(self.now, minutes=-6).isoformat())
		self.assertEqual(self.signal("programador")["status"], "ok")

	def test_a_beat_older_than_one_and_three_quarter_ticks_is_red(self):
		beat._write(add_to_date(self.now, minutes=-8).isoformat())
		got = self.signal("programador")
		self.assertEqual(got["status"], "fail")
		self.assertIn("hace 8 min", got["detail"])
		self.assertIn("7 min", got["detail"])

	def test_a_short_tick_never_goes_below_five_minutes(self):
		self.tick.return_value = 60
		beat._write(add_to_date(self.now, minutes=-4).isoformat())
		self.assertEqual(self.signal("programador")["status"], "ok")
		beat._write(add_to_date(self.now, minutes=-6).isoformat())
		self.assertEqual(self.signal("programador")["status"], "fail")

	def test_the_threshold_comes_from_site_config(self):
		beat._write(add_to_date(self.now, minutes=-8).isoformat())
		self.conf["hr_health_scheduler_minutes"] = 10
		self.assertEqual(self.signal("programador")["status"], "ok")

	def test_the_tick_is_frappes(self):
		self._stack.enter_context(patch.dict(frappe.local.conf, {"scheduler_tick_interval": 90}))
		self.assertEqual(self.real_tick(), 90)

	def test_no_beat_ever_is_red(self):
		os.remove(beat.path())
		self.assertEqual(self.signal("programador")["status"], "fail")

	def test_a_paused_or_disabled_scheduler_is_red_at_once(self):
		self.inactive.return_value = True
		got = self.signal("programador")
		self.assertEqual(got["status"], "fail")
		self.assertIn("apagado", got["detail"])


class RevisionsTestCase(HealthCase):
	def revision(self, conn: str, status: str, minutes: int) -> str:
		name = frappe.get_doc({"doctype": "Channel Booking Revision", "connection": conn, "status": status,
		                       "revision_id": frappe.generate_hash(length=10)}).insert(ignore_permissions=True).name
		self.backdate("Channel Booking Revision", name, minutes)
		return name

	def test_without_an_active_channex_connection_it_is_off(self):
		self.assertEqual(self.signal("revisiones")["status"], "off")

	def test_acked_and_recent_revisions_are_green(self):
		conn = self.connection()
		self.revision(conn, "Acked", 60)
		self.revision(conn, "Failed", 5)
		self.assertEqual(self.signal("revisiones")["status"], "ok")

	def test_a_revision_unacked_for_fifteen_minutes_is_red(self):
		conn = self.connection()
		for status in ("Failed", "Applied", "Received"):
			self.revision(conn, status, 16)
		got = self.signal("revisiones")
		self.assertEqual(got["status"], "fail")
		self.assertIn("3 revisiones", got["detail"])
		self.assertIn("hace 16 min", got["detail"])

	def test_an_inactive_connection_does_not_count(self):
		conn = self.connection()
		self.revision(conn, "Failed", 30)
		frappe.db.set_value("Channel Manager Connection", conn, "active", 0)
		self.connection(external_property_id="cx-otra")
		self.assertEqual(self.signal("revisiones")["status"], "ok")


class AriTestCase(HealthCase):
	def outbox(self, conn: str, status: str, minutes: int, **fields) -> str:
		today = frappe.utils.nowdate()
		values = {"doctype": "Channel ARI Outbox", "connection": conn, "property": PROP, "status": status,
		          "kinds": "availability", "date_from": today, "date_to": today, "detail": "Rejected: x"}
		name = frappe.get_doc({**values, **fields}).insert(ignore_permissions=True).name
		self.backdate("Channel ARI Outbox", name, minutes, "modified")
		return name

	def test_without_an_active_channex_connection_it_is_off(self):
		self.assertEqual(self.signal("ari")["status"], "off")

	def test_sent_rows_and_an_ok_push_are_green(self):
		conn = self.connection(last_push_status="OK: 3 availability, 0 restrictions")
		self.outbox(conn, "Sent", 5)
		self.outbox(conn, "Failed", 60 * 25)  # de antes de ayer: ya pasó el barrido de la noche
		self.assertEqual(self.signal("ari")["status"], "ok")

	def test_a_failed_row_of_the_last_day_is_red(self):
		conn = self.connection(last_push_status="OK: 1 availability, 0 restrictions")
		self.outbox(conn, "Failed", 30)
		got = self.signal("ari")
		self.assertEqual(got["status"], "fail")
		self.assertIn("1 envío", got["detail"])

	def test_a_failed_last_push_is_red(self):
		self.connection(last_push_status="FAILED: restrictions: 422 invalid rate for 2026-12-24")
		got = self.signal("ari")
		self.assertEqual(got["status"], "fail")
		self.assertIn("422", got["detail"])
		self.assertNotIn("2026-12-24", got["detail"])  # el detalle pasa por el filtro de datos

	def test_a_full_sync_sent_after_the_failure_clears_it(self):
		conn = self.connection(last_push_status="OK: 500 availability, 500 restrictions")
		self.outbox(conn, "Failed", 30)
		full = self.outbox(conn, "Sent", 5, full=1, reason="Full sync")
		frappe.db.set_value("Channel ARI Outbox", full, "sent_at", add_to_date(self.now, minutes=-5),
		                    update_modified=False)
		self.assertEqual(self.signal("ari")["status"], "ok")


class CfeTestCase(HealthCase):
	def setUp(self):
		super().setUp()
		make_uy_property()
		gateway_settings()

	def row(self, status: str, minutes: int, core_status: str | None = None) -> str:
		doc = frappe.get_doc({"doctype": "UY CFE", "kind": "Original", "status": status, "property": PROP,
		                      "core_status": core_status})
		doc.flags.ignore_validate = True
		name = doc.insert(ignore_permissions=True).name
		self.backdate("UY CFE", name, minutes)
		return name

	def test_nothing_pending_is_green(self):
		self.row("Sent", 60, "issued")
		self.row("Pending", 5)
		self.core.cfes["queued"] = [_cfe("queued", 5)]
		got = self.signal("cfe")
		self.assertEqual(got["status"], "ok", got)
		self.assertTrue(any("status=queued" in url for _m, url, _h in self.core.calls))
		self.assertTrue(any("status=contingency" in url for _m, url, _h in self.core.calls))

	def test_kamra_rows_pending_too_long_or_needing_attention_are_red(self):
		self.row("Pending", 31)
		self.row("Needs attention", 1)
		got = self.signal("cfe")
		self.assertEqual(got["status"], "fail")
		self.assertIn("1 sin mandar a core", got["detail"])
		self.assertIn("1 a revisar", got["detail"])

	def test_core_queued_too_long_or_in_contingency_is_red(self):
		self.core.cfes["queued"] = [_cfe("queued", 45), _cfe("queued", 2)]
		self.core.cfes["contingency"] = [_cfe("contingency", 10, overdue=True)]
		got = self.signal("cfe")
		self.assertEqual(got["status"], "fail")
		self.assertIn("1 en cola", got["detail"])
		self.assertIn("1 en contingencia", got["detail"])
		self.assertIn("fuera de plazo", got["detail"])
		key = next(h.get("Authorization") for _m, url, h in self.core.calls if "/v1/fiscal/cfe" in url)
		self.assertTrue(key.startswith("Bearer "))

	def test_core_not_answering_is_red(self):
		self.core.down = True
		got = self.signal("cfe")
		self.assertEqual(got["status"], "fail")
		self.assertIn("core", got["detail"])

	def test_a_key_without_read_scope_says_so(self):
		self.core.list_status = 403
		got = self.signal("cfe")
		self.assertEqual(got["status"], "fail")
		self.assertIn("forbidden", got["detail"])

	def test_without_fiscal_configuration_it_is_off(self):
		frappe.db.delete("Payment Gateway Settings")
		self.assertEqual(self.signal("cfe")["status"], "off")


class ErrorsTestCase(HealthCase):
	def test_few_errors_are_green(self):
		for i in range(4):
			frappe.log_error(title=f"UT salud {i}", message="x")
		got = self.signal("errores")
		self.assertEqual(got["status"], "ok")
		self.assertIn("4 errores", got["detail"])

	def test_five_errors_in_the_last_hour_are_red_with_the_titles_scrubbed(self):
		for _ in range(5):
			frappe.log_error(title="Reserva de juana@correo.test falló", message="x")
		old = frappe.log_error(title="UT salud vieja", message="x")
		self.backdate("Error Log", old.name, 61)
		got = self.signal("errores")
		self.assertEqual(got["status"], "fail")
		self.assertIn("5 errores", got["detail"])
		self.assertIn("5 veces", got["detail"])
		self.assertNotIn("juana", got["detail"])


class DiskTestCase(HealthCase):
	def test_below_ninety_per_cent_is_green(self):
		self.disk.return_value = (100 * 2**30, 89 * 2**30, 11 * 2**30)
		self.assertEqual(self.signal("disco")["status"], "ok")

	def test_ninety_per_cent_is_red(self):
		self.disk.return_value = (100 * 2**30, 90 * 2**30, 10 * 2**30)
		got = self.signal("disco")
		self.assertEqual(got["status"], "fail")
		self.assertIn("90 %", got["detail"])
		self.assertIn("10.0 GB libres", got["detail"])

	def test_it_measures_the_site_folder(self):
		self.disk.return_value = (100, 1, 99)
		self.signal("disco")
		self.assertEqual(self.disk.call_args.args[0], frappe.get_site_path())


class CoreTestCase(HealthCase):
	def test_core_healthy_is_green(self):
		self.assertEqual(self.signal("core")["status"], "ok")

	def test_core_degraded_is_red(self):
		self.core.health = (503, {"status": "degraded", "db": "down", "queue": "ok", "version": "t"})
		got = self.signal("core")
		self.assertEqual(got["status"], "fail")
		self.assertIn("base de datos down", got["detail"])

	def test_core_down_is_red(self):
		self.core.down = True
		self.assertEqual(self.signal("core")["status"], "fail")

	def test_without_core_it_is_off(self):
		self.conf["hr_core_base_url"] = ""
		self.assertEqual(self.signal("core")["status"], "off")


class ReportTestCase(HealthCase):
	def test_every_signal_is_there(self):
		report = checks.report(self.now)
		self.assertEqual(set(report["signals"]),
		                 {"programador", "revisiones", "ari", "cfe", "errores", "disco", "core"})
		self.assertEqual(report["status"], "ok")

	def test_one_red_signal_makes_it_red(self):
		self.disk.return_value = (100, 95, 5)
		self.assertEqual(checks.report(self.now)["status"], "fail")

	def test_a_check_that_breaks_is_red_not_a_500(self):
		with patch.object(checks, "_disk_usage", side_effect=PermissionError("x")):
			got = checks.report(self.now)
		self.assertEqual(got["signals"]["disco"]["status"], "fail")
		self.assertIn("PermissionError", got["signals"]["disco"]["detail"])


class EndpointTestCase(HealthCase):
	def call(self, token: str | None = None, user: str = "Guest") -> dict:
		frappe.set_user(user)  # nosemgrep: frappe-setuser -- test fixture
		headers = {checks.TOKEN_HEADER: token} if token else {}
		with patch.object(frappe, "get_request_header", side_effect=lambda key, default=None: headers.get(key, default)):
			return health.status()

	def test_it_is_whitelisted_for_guests_and_only_for_get(self):
		self.assertIn(health.status, frappe.guest_methods)
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[health.status], ["GET"])

	def test_a_guest_sees_only_the_states(self):
		self.conf["hr_health_token"] = TOKEN
		got = self.call()
		self.assertEqual(got["status"], "ok")
		self.assertEqual(got["signals"]["programador"], {"status": "ok"})
		self.assertNotIn("detail", json.dumps(got))

	def test_the_token_shows_the_details(self):
		self.conf["hr_health_token"] = TOKEN
		got = self.call(TOKEN)
		self.assertIn("detail", got["signals"]["programador"])
		self.assertNotIn("detail", json.dumps(self.call("otro-token")))

	def test_no_token_configured_means_no_details_for_anybody(self):
		self.conf.pop("hr_health_token", None)
		self.assertNotIn("detail", json.dumps(self.call("")))

	def test_a_system_manager_sees_the_details(self):
		self.assertIn("detail", self.call(user="Administrator")["signals"]["disco"])

	def test_the_report_is_cached_for_a_while(self):
		self.call()
		self.disk.return_value = (100, 95, 5)
		self.assertEqual(self.call()["status"], "ok")
		frappe.cache.delete_value(checks.CACHE_KEY)
		self.assertEqual(self.call()["status"], "fail")


class BeatTestCase(HealthCase):
	def test_the_beat_writes_the_time_and_reads_it_back(self):
		beat.heartbeat()
		last = beat.last()
		self.assertLess(abs((now_datetime() - last).total_seconds()), 5)

	def test_a_broken_file_is_no_beat(self):
		with open(beat.path(), "w") as f:
			f.write("basura")
		self.assertIsNone(beat.last())

	def test_the_real_path_is_in_the_site_folder(self):
		real = self.real_beat_path()
		self.assertEqual(os.path.basename(real), beat.FILE)
		self.assertEqual(os.path.abspath(os.path.dirname(real)), os.path.abspath(frappe.get_site_path()))
