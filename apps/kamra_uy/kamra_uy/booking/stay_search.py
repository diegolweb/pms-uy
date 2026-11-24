"""HR-337: la cotización pública (kamra.public_api.search_stay) protegida por lo que CUESTA, no por las noches.

Desde HR-322 (D-HR-49) la reserva en línea no tiene tope de noches ni de fechas, y search_stay es de invitado. Medido el
24-11-2026 en un stack efímero (4 tipos de habitación x 3 planes de comida, `local/hr/ola4/FIN-HR-337.md`): la memoria no
pesa (13 MiB de pico a 10 años), pero el tiempo crece con las noches, ~10,5 ms de CPU por noche y ~48 consultas a la base
(30 noches: 0,6 s; 1 año: 4,3 s; 10 años: 38 s, y el doble con un código de descuento, que se cotiza dos veces). El
backend de producción tiene 2 procesos de 4 hilos: unas pocas búsquedas de años a la vez dejarían sin respuesta a
recepción y a los huéspedes. El borde (Traefik, hrprod-tope-publica) cuenta peticiones, 5 por segundo: no ve el peso.

Dos límites, los dos por coste («unidades» = noches, x2 con código de descuento), y ninguno corta noches ni fechas:
  · PRESUPUESTO por IP: 60.000 unidades por hora. La web hace como mucho 7 búsquedas por página (la estadía y, si no hay
    nada libre, hasta 6 cercanas, cada una con sus noches): una reserva de 37 noches gasta 259 y una de un año, 2.555.
    Caben 100 huéspedes por hora detrás de la misma IP (el CGNAT de los móviles) aunque cada uno pruebe diez estadías de
    7 noches y en todas le salgan las cercanas (49.000). Lo que sí frena: que una IP queme más de ~10 min de CPU por
    hora (16 búsquedas de 10 años).
  · RÁFAGA, para todo el sitio: como mucho 2 búsquedas PESADAS (más de 400 unidades, unos 4 s) calculándose a la vez.
    Con 2 procesos, siempre queda CPU para recepción; una búsqueda corta nunca espera por esto.
El que pasa de uno u otro recibe el 429 de Frappe («demasiadas peticiones»), que la web ya enseña como «Hiciste muchos
intentos seguidos…» (error.limite) sin texto nuevo. Lo rechazado no gasta presupuesto.

Consecuencia que hay que decir: una sola búsqueda que cueste más que el presupuesto entero (más de 60.000 noches, ~164
años, o 30.000 con código) nunca se cotiza en línea. No es un tope de venta, pero es el techo implícito de cualquier
límite por coste.

Solo cuenta en peticiones HTTP (como el rate_limit de Frappe): recepción, el MCP por consola, las pruebas y la propia
Kamra que llamen a la función por dentro no pasan por aquí. La IP es frappe.local.request_ip, que en producción es la de
la conexión (el borde reescribe X-Forwarded-For: lo prueba deploy/prod/hardening/tests/nivel3.sh)."""

from __future__ import annotations

import time
from contextlib import ExitStack, contextmanager

import frappe
from frappe.utils import date_diff

WINDOW_SECONDS = 3600
BUDGET_PER_IP = 60_000  # unidades por IP y hora
HEAVY_UNITS = 400  # a partir de aquí una búsqueda ocupa una plaza de ráfaga
HEAVY_SLOTS = 2  # búsquedas pesadas a la vez, para todo el sitio
SLOT_TTL = 900  # una plaza que nadie suelta (un proceso muerto) caduca sola

def units(check_in_date, check_out_date, voucher_code=None) -> int:
	"""Lo que cuesta cotizar esta estadía: sus noches, el doble con código (se cotiza con y sin él). 0 si las fechas no
	sirven: search_stay da su propio error sin cotizar nada."""
	try:
		nights = date_diff(check_out_date, check_in_date)
	except Exception:
		return 0
	if nights < 1:
		return 0
	return nights * (2 if (voucher_code or "").strip() else 1)


def _refuse():
	# el texto exacto del rate_limit de Frappe, que ya trae su traducción: ningún texto nuevo
	frappe.throw(frappe._("You hit the rate limit because of too many requests. Please try after sometime."),
	             frappe.RateLimitExceededError)


def _charge(ip: str, cost: int) -> str:
	key = frappe.cache.make_key(f"kamra_uy:search_stay:budget:{ip}")
	used = frappe.cache.incrby(key, cost)
	if used == cost or frappe.cache.ttl(key) < 0:
		frappe.cache.expire(key, WINDOW_SECONDS)
	if used > BUDGET_PER_IP:
		frappe.cache.decrby(key, cost)
		_refuse()
	return key


@contextmanager
def _heavy_slot():
	key = frappe.cache.make_key("kamra_uy:search_stay:heavy")
	token = frappe.generate_hash(length=16)
	now = time.time()
	pipe = frappe.cache.pipeline()
	pipe.zremrangebyscore(key, 0, now - SLOT_TTL)
	pipe.zadd(key, {token: now})
	pipe.zcard(key)
	pipe.expire(key, SLOT_TTL)
	taken = pipe.execute()[2]
	if taken > HEAVY_SLOTS:
		frappe.cache.zrem(key, token)
		_refuse()
	try:
		yield
	finally:
		frappe.cache.zrem(key, token)


@contextmanager
def guard(ip: str | None, cost: int):
	"""Cobra `cost` del presupuesto de `ip` y, si es pesada, toma una plaza de ráfaga mientras dura. Sin IP o sin coste
	no hace nada."""
	if not ip or cost < 1:
		yield
		return
	key = _charge(ip, cost)
	with ExitStack() as stack:
		if cost > HEAVY_UNITS:
			try:
				stack.enter_context(_heavy_slot())
			except BaseException:
				frappe.cache.decrby(key, cost)  # rechazada por la ráfaga: no gasta presupuesto
				raise
		yield


@frappe.whitelist(allow_guest=True)
def search_stay(property: str, check_in_date: str, check_out_date: str, **kwargs):
	"""kamra.public_api.search_stay tal cual, con el presupuesto y la ráfaga de arriba en las peticiones HTTP."""
	from kamra import public_api

	ip = getattr(frappe.local, "request_ip", None) if getattr(frappe.local, "request", None) else None
	cost = units(check_in_date, check_out_date, kwargs.get("voucher_code"))
	with guard(ip, cost):
		return frappe.call(public_api.search_stay, property=property, check_in_date=check_in_date,
		                   check_out_date=check_out_date, **kwargs)
