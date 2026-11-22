"""El filtro de lo que sale hacia Sentry (HR-21): ningún dato personal de un huésped, venga en el mensaje del error, en la
petición, en las variables de la traza o en el usuario. Sin frappe: corre en `bench run-tests` y con
`python3 -m unittest` desde esta carpeta."""

from __future__ import annotations

import copy
import json

try:
	from kamra_uy.health.tests._local import UnitTestCase, load
except ImportError:  # fuera del bench
	from _local import UnitTestCase, load

scrub = load("scrub")

# Datos de mentira con la forma de los de verdad: correo, móvil uruguayo, cédula, tarjeta, nombre, fecha de nacimiento.
EMAIL = "juana.perez@correo.test"
PHONE = "+598 99 123 456"
CI = "1.234.567-8"
CARD = "4509 9535 6623 3704"
NAME = "Juana Pérez"
BIRTH = "1984-03-07"
PII = (EMAIL, "juana.perez", "99 123 456", "123 456", "1.234.567", "4509", "3704", "Juana", "Pérez", BIRTH)


def _leaks(value) -> list[str]:
	blob = json.dumps(value, ensure_ascii=False, default=str)
	return [p for p in PII if p in blob]


class ScrubTextTestCase(UnitTestCase):
	def test_an_email_is_masked(self):
		out = scrub.text(f"no pude escribir a {EMAIL}")
		self.assertNotIn("juana", out)
		self.assertIn("[correo]", out)

	def test_phone_document_card_and_dates_are_masked(self):
		for raw in (PHONE, "099123456", CI, CARD, BIRTH, "220645600013"):
			out = scrub.text(f"valor {raw} fin")
			self.assertEqual(out, "valor [número] fin", raw)

	def test_short_numbers_stay(self):
		self.assertEqual(scrub.text("line 123 · 3 noches · 10 %"), "line 123 · 3 noches · 10 %")

	def test_quoted_and_bold_text_is_masked(self):
		for raw in (f"Guest '{NAME}' not found", f'Guest "{NAME}" not found', f"<strong>{NAME}</strong> ya existe",
		            f"<b>{NAME}</b> ya existe"):
			out = scrub.text(raw)
			self.assertNotIn("Juana", out, raw)
			self.assertIn("[texto]", out, raw)

	def test_file_paths_and_dotted_names_in_quotes_stay(self):
		line = 'File "apps/kamra/kamra/api.py", line 12, in book: frappe.get_attr(\'kamra.public_api.book\')'
		self.assertEqual(scrub.text(line), line)

	def test_a_single_quoted_word_is_masked(self):
		self.assertNotIn("Juana", scrub.text("Guest 'Juana' not found"))

	def test_secrets_after_a_key_are_masked(self):
		out = scrub.text("GET /x?api_key=abc123def&token=zz9yy8&sid=s3cr3t5 password: hunter22")
		for secret in ("abc123def", "zz9yy8", "s3cr3t5", "hunter22"):
			self.assertNotIn(secret, out)

	def test_none_and_non_text(self):
		self.assertEqual(scrub.text(None), "")
		self.assertEqual(scrub.text(12), "12")


def _event() -> dict:
	"""Un evento como el que arma sentry-sdk con la integración de Frappe (frappe/utils/sentry.py) en un error de una
	petición con cuerpo JSON: usuario, petición, contexto «JSON Body», migas con SQL y variables locales."""
	return {
		"event_id": "e1", "level": "error", "platform": "python", "timestamp": "2026-11-21T10:00:00Z",
		"server_name": "backend-1", "release": "kamra@abc", "environment": "produccion",
		"user": {"id": "reservas.example.com", "email": EMAIL, "ip_address": "203.0.113.7"},
		"tags": {"frappe_user": EMAIL, "error_log": "el-1", "titulo": f"Guest '{NAME}'", "otra": "x"},
		"extra": {"job": {"kwargs": {"guest": NAME, "phone": PHONE}}},
		"contexts": {
			"JSON Body": {"guest_name": NAME, "email": EMAIL},
			"Form Data": {"phone": PHONE},
			"runtime": {"name": "CPython", "version": "3.14.10"},
			"os": {"name": "Linux"},
			"trace": {"trace_id": "t1", "span_id": "s1"},
		},
		"request": {"method": "POST", "url": f"https://reservas.example.com/api/method/kamra.api.book?email={EMAIL}",
		            "query_string": f"email={EMAIL}", "data": {"guest": NAME}, "cookies": {"sid": "x"},
		            "headers": {"Cookie": "sid=x", "X-Guest": NAME}, "env": {"REMOTE_ADDR": "203.0.113.7"}},
		"breadcrumbs": {"values": [{"category": "query", "message": f"select * from tabGuest where email='{EMAIL}'"}]},
		"exception": {"values": [{
			"type": "ValidationError", "module": "frappe.exceptions",
			"value": f"Guest {EMAIL} ({PHONE}) not found: <strong>{NAME}</strong>",
			"mechanism": {"type": "wsgi", "handled": False},
			"stacktrace": {"frames": [{
				"filename": "apps/kamra/kamra/api.py", "abs_path": "/home/frappe/frappe-bench/apps/kamra/kamra/api.py",
				"function": "book", "module": "kamra.api", "lineno": 120, "context_line": "    guest = _guest(data)",
				"pre_context": ["def book(data):"], "post_context": ["    return guest"],
				"vars": {"data": {"email": EMAIL, "card": CARD}, "name": NAME}, "in_app": True}]},
		}]},
		"threads": {"values": [{"id": 1, "stacktrace": {"frames": [{"function": "run", "vars": {"x": NAME}}]}}]},
		"logentry": {"message": f"Error con {EMAIL}", "params": [NAME]},
		"message": f"Error con {CI}",
		"modules": {"frappe": "16.0.0"},
		"transaction": f"/api/method/kamra.api.book?email={EMAIL}",
		"fingerprint": ["abc123"],
	}


class ScrubEventTestCase(UnitTestCase):
	def test_nothing_personal_survives(self):
		self.assertEqual(_leaks(scrub.event(_event(), {})), [])

	def test_the_original_event_is_not_changed(self):
		original = _event()
		kept = copy.deepcopy(original)
		scrub.event(original, {})
		self.assertEqual(original, kept)

	def test_what_helps_to_fix_the_error_stays(self):
		out = scrub.event(_event(), {})
		exc = out["exception"]["values"][0]
		self.assertEqual(exc["type"], "ValidationError")
		frame = exc["stacktrace"]["frames"][0]
		self.assertEqual((frame["filename"], frame["function"], frame["lineno"]), ("apps/kamra/kamra/api.py", "book", 120))
		self.assertEqual(frame["context_line"], "    guest = _guest(data)")
		self.assertNotIn("vars", frame)
		self.assertNotIn("value", exc)  # el mensaje de una excepción es texto libre: no sale (el Error Log lo tiene)
		for key in ("event_id", "level", "server_name", "release", "environment", "fingerprint", "timestamp"):
			self.assertEqual(out[key], _event()[key], key)
		self.assertEqual(set(out["contexts"]), {"runtime", "os", "trace"})

	def test_user_breadcrumbs_extra_threads_and_modules_go(self):
		out = scrub.event(_event(), {})
		for key in ("user", "breadcrumbs", "extra", "threads", "modules"):
			self.assertNotIn(key, out)

	def test_the_request_keeps_only_its_method_and_path(self):
		out = scrub.event(_event(), {})
		self.assertEqual(out["request"], {"method": "POST", "url": "https://reservas.example.com/api/method/kamra.api.book"})
		self.assertEqual(out["transaction"], "/api/method/kamra.api.book")

	def test_only_known_tags_stay_and_scrubbed(self):
		out = scrub.event(_event(), {})
		self.assertEqual(set(out["tags"]), {"error_log", "titulo"})
		self.assertNotIn("Juana", out["tags"]["titulo"])

	def test_a_message_event_is_scrubbed(self):
		out = scrub.event({"event_id": "e2", "level": "error", "message": f"Fallo {EMAIL}",
		                   "logentry": {"message": f"Fallo {PHONE}", "formatted": f"Fallo {CARD}"}}, {})
		self.assertEqual(_leaks(out), [])
		self.assertEqual(out["message"], "Fallo [correo]")


TRACEBACK = """Traceback (most recent call last):
  File "apps/frappe/frappe/utils/background_jobs.py", line 300, in execute_job
    retval = method(**kwargs)
  File "apps/frappe/frappe/model/document.py", line 120, in get_doc
    raise frappe.DoesNotExistError(doctype=doctype)
frappe.exceptions.DoesNotExistError: Guest Juana Pérez not found

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "apps/kamra/kamra/api.py", line 9, in book
    guest = get(name)
      name = 'Juana Pérez'
KeyError: 'Juana Pérez'
"""


class ScrubTracebackTestCase(UnitTestCase):
	def test_only_the_structure_of_a_traceback_stays(self):
		out = scrub.traceback(TRACEBACK)
		self.assertNotIn("Juana", out)
		self.assertIn('  File "apps/frappe/frappe/model/document.py", line 120, in get_doc', out)
		self.assertIn("    raise frappe.DoesNotExistError(doctype=doctype)", out)
		self.assertIn("frappe.exceptions.DoesNotExistError", out)
		self.assertIn("KeyError", out)
		self.assertIn("During handling of the above exception, another exception occurred:", out)
		self.assertNotIn("name = ", out)  # una variable volcada (developer_mode) no sale

	def test_a_title_that_is_the_exception_message_becomes_its_type(self):
		out = scrub.message(f"Guest Juana Pérez not found\n\n{TRACEBACK}")
		self.assertEqual(out.splitlines()[0], "frappe.exceptions.DoesNotExistError")
		self.assertNotIn("Juana", out)

	def test_a_written_title_stays_scrubbed(self):
		out = scrub.message(f"Night audit alert (email) failed: {EMAIL}\n\n{TRACEBACK}")
		self.assertEqual(out.splitlines()[0], "Night audit alert (email) failed: [correo]")

	def test_a_message_event_keeps_no_exception_text(self):
		out = scrub.event({"event_id": "e3", "message": f"Guest Juana Pérez not found\n{TRACEBACK}",
		                   "logentry": {"message": f"x\n{TRACEBACK}"}}, {})
		self.assertEqual(_leaks(out), [])
		self.assertIn("DoesNotExistError", out["message"])

	def test_title_helper(self):
		self.assertEqual(scrub.title("Guest Juana Pérez not found", TRACEBACK), "frappe.exceptions.DoesNotExistError")
		self.assertEqual(scrub.title(f"Reserva de {EMAIL}", TRACEBACK), "Reserva de [correo]")
		self.assertEqual(scrub.title(None, ""), "")
