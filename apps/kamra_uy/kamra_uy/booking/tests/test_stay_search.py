"""HR-337: el presupuesto por coste de la cotización pública (kamra_uy/booking/stay_search.py) nunca le quita una venta a
un huésped de verdad, tampoco a muchos detrás de la misma IP, y sí corta a quien pide años de cotización en bucle. HR-353:
lo que se calcula a la vez queda acotado también por debajo de 400 noches (400 por IP, 1.200 en el sitio), sin que una
estadía corta ni la estadía con sus 6 cercanas choquen nunca.

Lo corre `bench run-tests --module kamra_uy.booking.tests.test_stay_search`.
"""

from __future__ import annotations

import time
import uuid
from contextlib import ExitStack
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
		self.assertEqual(frappe.cache.zcard(stay_search.flight_keys(self.ip)[1]), 0)  # ni la de noches en vuelo


class TestStaySearchInFlight(AuditTestCase):
	"""HR-353: las noches en vuelo. Las búsquedas abiertas a la vez se simulan con guardas anidadas: es lo que ven Redis
	y los dos procesos (la plaza se toma al entrar y se suelta al salir)."""

	properties = ()

	def setUp(self):
		super().setUp()
		base = uuid.uuid4().int % 250
		self.ips = [f"198.51.100.{base + i + 1}" for i in range(4)] + [f"2001:db8::{uuid.uuid4().hex[:4]}"]  # TEST-NET-2
		for ip in self.ips:
			for key in (frappe.cache.make_key(f"kamra_uy:search_stay:budget:{ip}"), stay_search.flight_keys(ip)[1]):
				frappe.cache.delete(key)
				self.addCleanup(frappe.cache.delete, key)
		self.site = stay_search.flight_keys(self.ips[0])[0]

	def used(self, ip: str) -> int:
		return int(frappe.cache.get(frappe.cache.make_key(f"kamra_uy:search_stay:budget:{ip}")) or 0)

	def assertRefused(self, ip: str, cost: int):
		before = self.used(ip)
		with self.assertRaises(frappe.RateLimitExceededError):
			with stay_search.guard(ip, cost):
				self.fail(f"{cost} unidades desde {ip} no debían calcularse ahora")
		self.assertEqual(self.used(ip), before)  # lo rechazado no gasta

	def test_una_ip_no_calcula_mas_de_400_noches_a_la_vez_y_las_demas_siguen(self):
		"""Con una de 400 noches calculándose, otra de 20 desde esa IP da 429 sin gastar; una de 14 (dos semanas) sí
		pasa, y otra de 400 desde otra IP también. Al salir no queda nada en vuelo."""
		me, other = self.ips[0], self.ips[1]
		with stay_search.guard(me, 400):
			self.assertRefused(me, 20)
			with stay_search.guard(me, 14):
				pass
			with stay_search.guard(other, 400):
				pass
		self.assertEqual(frappe.cache.zcard(stay_search.flight_keys(me)[1]), 0)
		self.assertEqual(self.used(me), 414)

	def test_el_sitio_no_calcula_mas_de_1200_a_la_vez_y_lo_corto_nunca_choca(self):
		"""Tres IPs con una de 400 cada una (dos de ellas pesadas, de 10 años, que cuentan 400) llenan el sitio: una de 15
		desde una cuarta IP da 429 sin gastar; la estadía corta (7 noches, o 7 con código = 14) pasa igual. Al soltarse
		una, la de 15 ya pasa."""
		ten_years = stay_search.units(d(10), d(10 + 3650))
		fourth = self.ips[3]
		with ExitStack() as held:
			held.enter_context(stay_search.guard(self.ips[0], ten_years))
			held.enter_context(stay_search.guard(self.ips[1], ten_years))
			with stay_search.guard(self.ips[2], 400):
				self.assertRefused(fourth, 15)
				with stay_search.guard(fourth, stay_search.units(d(10), d(17), "CODIGO")):
					pass
			with stay_search.guard(fourth, 15):
				pass
		self.assertEqual(frappe.cache.zcard(self.site), 0)

	def test_una_ip_no_abre_mas_de_6_a_la_vez(self):
		"""Seis de 20 noches a la vez desde una IP caben (las cercanas de la web); la séptima da 429 sin gastar, aunque
		sus noches cupieran, y una de 14 (que no cuenta) sigue pasando."""
		me = self.ips[0]
		with ExitStack() as held:
			for _ in range(stay_search.SEARCHES_PER_IP):
				held.enter_context(stay_search.guard(me, 20))
			self.assertRefused(me, 20)
			with stay_search.guard(me, 14):
				pass
		self.assertEqual(self.used(me), 6 * 20 + 14)

	def test_la_estadia_y_sus_6_cercanas_en_paralelo_caben_hasta_66_noches(self):
		"""La web pide la estadía sola y, si no hay nada libre, las 6 cercanas a la vez: de 66 noches caben las 6 (396
		en vuelo), y tras una IP IPv6 igual. Lo mismo de 33 noches con código."""
		for ip, (ci, co, code) in ((self.ips[0], (d(10), d(76), None)), (self.ips[-1], (d(10), d(43), "CODIGO"))):
			cost = stay_search.units(ci, co, code)
			with stay_search.guard(ip, cost):
				pass
			with ExitStack() as nearby:
				for _ in range(6):
					nearby.enter_context(stay_search.guard(ip, cost))
			self.assertEqual(self.used(ip), 7 * cost)

	def test_la_plaza_de_un_proceso_muerto_caduca_sola(self):
		"""Tres plazas de 400 que un proceso muerto dejó tomadas y ya caducaron no cuentan: la siguiente pasa y las
		barre."""
		dead = {f"400:ut-{uuid.uuid4().hex}": time.time() - 1 for _ in range(3)}
		frappe.cache.zadd(self.site, dead)
		self.addCleanup(frappe.cache.zrem, self.site, *dead)
		with stay_search.guard(self.ips[0], 400):
			pass
		self.assertFalse(any(frappe.cache.zscore(self.site, m) for m in dead))


def _patched(name: str, value):
	from unittest.mock import patch

	return patch.object(frappe.local, name, value, create=True)
