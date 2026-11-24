"""HR-337: el presupuesto por coste de la cotización pública (kamra_uy/booking/stay_search.py) nunca le quita una venta a
un huésped de verdad, tampoco a muchos detrás de la misma IP, y sí corta a quien pide años de cotización en bucle.

Lo corre `bench run-tests --module kamra_uy.booking.tests.test_stay_search`.
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta

import frappe
from frappe.utils import getdate
from kamra.tests.audit_fixtures import AuditTestCase, d, make_property, make_room, make_room_type

from kamra_uy.booking import stay_search

PROP = "UT Busqueda Presupuesto"


class TestStaySearchBudget(AuditTestCase):
	properties = (PROP,)

	def setUp(self):
		super().setUp()
		make_property(PROP, booking_engine_enabled=1, booking_payment_mode="Pay at hotel", booking_mode="Instant",
		              minimum_nights=1, maximum_nights=0, cleaning_fee=0)
		self.rt = make_room_type(PROP, "BUS", base_price=100, adults_capacity=2)
		make_room(PROP, self.rt, "B101")
		form = frappe.local.form_dict
		self.addCleanup(setattr, frappe.local, "form_dict", form)
		self.ip = f"203.0.113.{uuid.uuid4().int % 254 + 1}"  # TEST-NET-3: la IP (compartida) de los huéspedes
		self.budget = frappe.cache.make_key(f"kamra_uy:search_stay:budget:{self.ip}")
		self.addCleanup(frappe.cache.delete, self.budget)

	def used(self) -> int:
		return int(frappe.cache.get(self.budget) or 0)

	def search(self, check_in: str, check_out: str) -> list:
		"""Un GET de la web a /api/method/kamra.public_api.search_stay, sin sesión y desde self.ip (como dispatch en
		test_residence): pasa por el hook de kamra_uy como en producción."""
		from frappe.handler import execute_cmd
		from frappe.utils import get_url
		from werkzeug.test import EnvironBuilder

		cmd = "kamra.public_api.search_stay"
		request = EnvironBuilder(method="GET", base_url=get_url(), path=f"/api/method/{cmd}",
		                         environ_base={"REMOTE_ADDR": self.ip}).get_request()
		frappe.set_user("Guest")  # nosemgrep: frappe-setuser -- la web busca sin sesión; tearDown vuelve a Administrator
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.local.form_dict = frappe._dict({"cmd": cmd, "property": PROP, "check_in_date": check_in,
		                                       "check_out_date": check_out, "adults": "2", "children": "0"})
		with _patched("request", request), _patched("request_ip", self.ip):
			return execute_cmd(cmd)

	def test_una_reserva_de_37_noches_con_todas_las_llamadas_de_la_web_se_cotiza(self):
		"""La web pide la estadía y, si no ve nada libre, hasta 6 cercanas (±1, ±2 y ±3 días, web/src/booking/reglas.ts
		fechasCercanas): las 7 se cotizan, con su precio, y gastan 7 x 37 unidades del presupuesto de la IP."""
		start = getdate(d(10))
		for shift in (0, -1, 1, -2, 2, -3, 3):
			ci = start + timedelta(days=shift)
			rows = self.search(str(ci), str(ci + timedelta(days=37)))
			self.assertTrue(rows and rows[0]["available"] and rows[0]["quote"], rows)
		self.assertEqual(self.used(), 7 * 37)

	def test_cien_huespedes_detras_de_una_misma_ip_nunca_chocan(self):
		"""El CGNAT de los móviles: 100 huéspedes en una hora desde una IP, cada uno prueba 10 estadías de 7 noches y en
		todas le salen las 6 cercanas (7 búsquedas): ninguna se rechaza."""
		start = getdate(d(10))
		for _ in range(100 * 10 * 7):
			with stay_search.guard(self.ip, stay_search.units(str(start), str(start + timedelta(days=7)))):
				pass
		self.assertEqual(self.used(), 49_000)

	def test_quien_pide_anios_en_bucle_se_corta_y_lo_corto_sigue(self):
		"""16 búsquedas de 10 años caben en la hora; la 17.ª da 429 y no gasta. Con las 2 plazas de búsquedas pesadas
		ocupadas, otra pesada da 429 sin gastar, y una corta (7 noches) sigue pasando."""
		ten_years = stay_search.units(d(10), d(10 + 3650))
		for _ in range(16):
			with stay_search.guard(self.ip, ten_years):
				pass
		before = self.used()
		with self.assertRaises(frappe.RateLimitExceededError):
			with stay_search.guard(self.ip, ten_years):
				self.fail("la 17.ª búsqueda de 10 años no debía cotizarse")
		self.assertEqual(self.used(), before)

		frappe.cache.delete(self.budget)
		heavy = frappe.cache.make_key("kamra_uy:search_stay:heavy")
		held = [f"ut-{uuid.uuid4().hex}" for _ in range(stay_search.HEAVY_SLOTS)]
		frappe.cache.zadd(heavy, {t: time.time() for t in held})
		self.addCleanup(frappe.cache.zrem, heavy, *held)
		with self.assertRaises(frappe.RateLimitExceededError):
			with stay_search.guard(self.ip, stay_search.units(d(10), d(10 + 500))):
				self.fail("una tercera búsqueda pesada a la vez no debía cotizarse")
		self.assertEqual(self.used(), 0)
		with stay_search.guard(self.ip, stay_search.units(d(10), d(17))):
			pass
		self.assertEqual(self.used(), 7)
		self.assertEqual(frappe.cache.zcard(heavy), stay_search.HEAVY_SLOTS)  # la rechazada no dejó su plaza


def _patched(name: str, value):
	from unittest.mock import patch

	return patch.object(frappe.local, name, value, create=True)
