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
  · NOCHES EN VUELO (HR-353): lo que se está calculando a la vez, contado en unidades (cada búsqueda cuenta sus unidades
    hasta 400; una pesada cuenta 400). Como mucho 400 por IP y 1.200 en todo el sitio, y no más de 6 búsquedas a la
    vez por IP (lo que la web abre junto: las 6 cercanas). Sin esto, una IP podía gastar
    su presupuesto en 150 búsquedas de 400 noches seguidas (~4 s cada una, dentro de las ráfagas del borde) y tener
    los 8 hilos del backend ocupados ~5 min por hora; con ~12 IPs, siempre. Ahora una IP tiene calculándose a la vez
    lo de una sola búsqueda de 400 y nunca los 8 hilos; el sitio entero, lo de 3 de 400.
    Las de hasta 14 unidades (dos semanas, o una con código; ~0,15 s) no cuentan: no retienen un hilo, son el grueso
    de las ventas y así ningún ataque, venga de cuantas IPs venga, puede rechazar una estadía corta. La web pide la
    estadía sola y, si no hay nada libre, 6 cercanas en paralelo: caben las 6 hasta 66 noches (33 con código); por
    encima puede rechazar alguna cercana, que la web cuenta como «sin hueco» (la estadía pedida nunca choca con eso).
El que pasa de uno u otro recibe el 429 de Frappe («demasiadas peticiones»), que la web ya enseña como «Hiciste muchos
intentos seguidos…» (error.limite) sin texto nuevo. Lo rechazado no gasta presupuesto ni deja plaza tomada; la plaza
de un proceso muerto caduca sola (2 min las de hasta 400 unidades, 15 min las pesadas).

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
FREE_UNITS = 14  # hasta aquí una búsqueda no cuenta en vuelo (dos semanas, ~0,15 s)
IN_FLIGHT_PER_IP = 400  # unidades calculándose a la vez desde una IP
SEARCHES_PER_IP = 6  # y búsquedas a la vez desde una IP: las 6 cercanas que la web abre juntas
IN_FLIGHT_SITE = 1_200  # unidades calculándose a la vez en todo el sitio
FLIGHT_TTL = 120  # la plaza en vuelo de una búsqueda no pesada (~4 s) de un proceso muerto

# Comprobar y tomar de una vez, en Redis: con dos procesos, dos búsquedas a la vez no pueden pasar las dos por el mismo
# hueco, y una rechazada no ocupa ni un instante lo que otra podía usar. Cada plaza es un miembro «<peso>:<ficha>» con
# su caducidad como puntuación; lo caducado se barre antes de sumar.
_TAKE_FLIGHT = """
local now = tonumber(ARGV[1])
local weight = tonumber(ARGV[4])
for i = 1, 2 do
  redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', now)
  local load = 0
  for _, m in ipairs(redis.call('ZRANGE', KEYS[i], 0, -1)) do
    load = load + (tonumber(string.match(m, '^(%d+):')) or 0)
  end
  if load + weight > tonumber(ARGV[4 + i]) then return 0 end
end
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[8]) then return 0 end
for i = 1, 2 do
  redis.call('ZADD', KEYS[i], ARGV[2], ARGV[3])
  redis.call('EXPIRE', KEYS[i], ARGV[7])
end
return 1
"""


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


def flight_keys(ip: str) -> tuple[str, str]:
	"""Las claves de las noches en vuelo: la del sitio y la de la IP."""
	return (frappe.cache.make_key("kamra_uy:search_stay:flight"),
	        frappe.cache.make_key(f"kamra_uy:search_stay:flight:{ip}"))


@contextmanager
def _in_flight(ip: str, cost: int):
	keys = flight_keys(ip)
	weight = min(cost, HEAVY_UNITS)
	member = f"{weight}:{frappe.generate_hash(length=16)}"
	now = time.time()
	expires = now + (SLOT_TTL if cost > HEAVY_UNITS else FLIGHT_TTL)
	if not frappe.cache.eval(_TAKE_FLIGHT, 2, *keys, now, expires, member, weight, IN_FLIGHT_SITE, IN_FLIGHT_PER_IP,
	                         SLOT_TTL, SEARCHES_PER_IP):
		_refuse()
	try:
		yield
	finally:
		for key in keys:
			frappe.cache.zrem(key, member)


@contextmanager
def guard(ip: str | None, cost: int):
	"""Cobra `cost` del presupuesto de `ip` y, mientras dura, toma su plaza en vuelo (si pasa de FREE_UNITS) y la de
	ráfaga (si es pesada). Lo rechazado devuelve lo cobrado. Sin IP o sin coste no hace nada."""
	if not ip or cost < 1:
		yield
		return
	key = _charge(ip, cost)
	with ExitStack() as stack:
		try:
			if cost > FREE_UNITS:
				stack.enter_context(_in_flight(ip, cost))
			if cost > HEAVY_UNITS:
				stack.enter_context(_heavy_slot())
		except BaseException:
			frappe.cache.decrby(key, cost)  # rechazada: no gasta presupuesto (y el ExitStack suelta lo ya tomado)
			raise
		yield


@frappe.whitelist(allow_guest=True)
def search_stay(property: str, check_in_date: str, check_out_date: str, **kwargs):
	"""kamra.public_api.search_stay tal cual, con el presupuesto, la ráfaga y las noches en vuelo de arriba en las
	peticiones HTTP."""
	from kamra import public_api

	ip = getattr(frappe.local, "request_ip", None) if getattr(frappe.local, "request", None) else None
	cost = units(check_in_date, check_out_date, kwargs.get("voucher_code"))
	with guard(ip, cost):
		return frappe.call(public_api.search_stay, property=property, check_in_date=check_in_date,
		                   check_out_date=check_out_date, **kwargs)
