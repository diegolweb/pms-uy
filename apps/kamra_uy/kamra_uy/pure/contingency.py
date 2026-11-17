"""Modo contingencia sin sincronización (HR-34): la regla de reparto de cupos entre extranets, sin frappe.

Mientras no hay channel manager, cada plataforma tiene en su extranet un cupo de habitaciones por tipo y fecha, y
recepción lo abre o lo cierra a mano. Por tipo y fecha, disponibles = libres - colchón; las plataformas se recorren por
prioridad (empates, por nombre) y cada una se abre con su cupo ENTERO si cabe en lo que queda, que se descuenta; si no
cabe, se cierra. Nunca se abre un cupo partido: así lo que pueden vender las extranets entre dos actualizaciones nunca
pasa de las libres.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

OPEN = "open"
CLOSE = "close"

# el informe diario sale a las 07:00 de la zona de la propiedad (la del hotel si no tiene)
DAILY_LOCAL_HOUR = 7
HOTEL_TIMEZONE = "America/Montevideo"

PlanKey = tuple[str, str, str]  # (fecha ISO, tipo de habitación, plataforma)


@dataclass(frozen=True)
class PlatformQuota:
	name: str  # "Booking.com"
	quota: int  # habitaciones de ese tipo en su extranet
	priority: int  # 1 = primera


@dataclass(frozen=True)
class Instruction:
	platform: str
	action: str  # "open" | "close"
	rooms: int  # lo que hay que poner en la extranet (0 si close)


def plan_day(free: int, platforms: list[PlatformQuota], buffer: int = 0) -> list[Instruction]:
	"""Qué poner en cada extranet un día para un tipo de habitación, en orden de prioridad."""
	if free < 0 or buffer < 0:
		raise ValueError(f"libres y colchón no pueden ser negativos (libres={free}, colchón={buffer})")
	left = max(0, free - buffer)
	plan = []
	for platform in sorted(platforms, key=lambda p: (p.priority, p.name)):
		if platform.quota < 0:
			raise ValueError(f"el cupo de {platform.name} no puede ser negativo ({platform.quota})")
		if 0 < platform.quota <= left:
			left -= platform.quota
			plan.append(Instruction(platform.name, OPEN, platform.quota))
		else:
			plan.append(Instruction(platform.name, CLOSE, 0))
	return plan


def changes(
	previous: dict[PlanKey, Instruction], current: dict[PlanKey, Instruction]
) -> list[tuple[PlanKey, Instruction | None, Instruction]]:
	"""Lo que difiere de `previous` a `current`, ordenado por clave; `None` como «antes» si la clave es nueva."""
	return [
		(key, previous.get(key), current[key]) for key in sorted(current) if previous.get(key) != current[key]
	]


def needs_alert(free: int, threshold: int) -> bool:
	"""Quedan pocas habitaciones: libres ≤ umbral."""
	return free <= threshold


# ---------------------------------------------------------------- piezas del informe y de los avisos


def parse_aliases(platform: str, aliases: str | None) -> frozenset[str]:
	"""Los textos de `Reservation.channel` que cuentan como esa plataforma: su nombre y sus alias, sin mayúsculas."""
	names = [platform, *(aliases or "").split(",")]
	return frozenset(n.strip().lower() for n in names if n.strip())


def match_platform(channel: str | None, aliases: dict[str, frozenset[str]]) -> str | None:
	"""La plataforma cuyo alias coincide con el `channel` de una reserva (texto libre), o None."""
	wanted = (channel or "").strip().lower()
	if not wanted:
		return None
	for platform in sorted(aliases):
		if wanted in aliases[platform]:
			return platform
	return None


def nights(check_in: date, check_out: date) -> list[date]:
	"""Las noches que ocupa una estancia; un uso de día (salida = entrada) ocupa su fecha, como en Kamra."""
	count = max((check_out - check_in).days, 1)
	return [check_in + timedelta(days=i) for i in range(count)]


def plan_to_json(plan: dict[PlanKey, Instruction]) -> dict[str, dict]:
	"""El plan como lo guarda `UY Contingency Snapshot.plan`: «fecha|tipo|plataforma» → acción y habitaciones."""
	return {"|".join(key): {"action": i.action, "rooms": i.rooms} for key, i in sorted(plan.items())}


def plan_from_json(stored) -> dict[PlanKey, Instruction]:
	"""Lee un plan guardado (dict o cadena JSON); lo que no tenga la forma esperada se descarta."""
	if isinstance(stored, str):
		try:
			stored = json.loads(stored) if stored else {}
		except ValueError:
			return {}
	plan: dict[PlanKey, Instruction] = {}
	for raw_key, value in (stored or {}).items():
		parts = raw_key.split("|")
		if len(parts) != 3 or not isinstance(value, dict):
			continue
		plan[(parts[0], parts[1], parts[2])] = Instruction(
			parts[2], value.get("action"), int(value.get("rooms") or 0)
		)
	return plan


def _day(day: date) -> str:
	return day.strftime("%d-%m")


def _left(free: int) -> str:
	return "queda 1" if free == 1 else f"quedan {free}"


def action_label(instruction: Instruction) -> str:
	"""Lo que la página pone en la celda de una extranet."""
	return f"Abrir con {instruction.rooms}" if instruction.action == OPEN else "Cerrar"


def instruction_text(instruction: Instruction, day: date, room_type: str, free: int) -> str:
	"""Una línea del aviso: «Cerrar Booking.com el 28-12 en Doble: quedan 3»."""
	if instruction.action == OPEN:
		verb = f"Abrir {instruction.platform} con {instruction.rooms}"
	else:
		verb = f"Cerrar {instruction.platform}"
	return f"{verb} el {_day(day)} en {room_type}: {_left(free)}"


def horizon_text(start: date, days: int) -> str:
	"""El título de la lista del informe diario: «… en los próximos 120 días, desde el 26-12:» (D-HR-30, punto 24)."""
	return f"Lo que debe tener cada extranet en los próximos {days} días, desde el {_day(start)}:"


def alert_text(day: date, room_type: str, free: int) -> str:
	"""La línea de «quedan pocas habitaciones»."""
	if free <= 0:
		return f"Sin habitaciones libres en {room_type} el {_day(day)}"
	return f"Últimas habitaciones: {_left(free)} en {room_type} el {_day(day)}"


def notice_hash(items) -> str:
	"""Huella de lo avisado para una fecha: no depende del orden, sí del contenido."""
	canonical = json.dumps(sorted(json.dumps(item, sort_keys=True, default=str) for item in items))
	return hashlib.sha256(canonical.encode()).hexdigest()


def split_recipients(text: str | None) -> list[str]:
	"""Los correos de `recipients`: separados por comas, punto y coma o saltos de línea."""
	return [r.strip() for r in re.split(r"[,;\n]", text or "") if r.strip()]


def is_daily_report_hour(now_utc: datetime, timezone_name: str | None) -> bool:
	"""¿Es la hora del informe diario en esa zona? El programador de Frappe evalúa los cron en la zona del SISTEMA, que
	puede no ser la del hotel: el trabajo corre cada hora y esta función decide."""
	try:
		zone = ZoneInfo(timezone_name or HOTEL_TIMEZONE)
	except (ZoneInfoNotFoundError, ValueError):
		zone = ZoneInfo(HOTEL_TIMEZONE)
	return now_utc.astimezone(zone).hour == DAILY_LOCAL_HOUR


# ---------------------------------------------------------------- configuración

MAX_HORIZON_DAYS = 365


@dataclass(frozen=True)
class PlatformRow:
	platform: str
	room_type: str
	quota: int
	priority: int


def config_errors(buffer: int, threshold: int, horizon: int, rows: list[PlatformRow]) -> list[str]:
	"""Lo que impide guardar `UY Contingency Settings`, en el orden de la pantalla (vacío = se puede guardar)."""
	errors = []
	if buffer < 0:
		errors.append("El colchón no puede ser negativo.")
	if threshold < 0:
		errors.append("El umbral de alerta no puede ser negativo.")
	if not 1 <= horizon <= MAX_HORIZON_DAYS:
		errors.append(f"El horizonte va de 1 a {MAX_HORIZON_DAYS} días.")
	seen: dict[tuple[str, str], tuple[int, str]] = {}
	for number, row in enumerate(rows, start=1):
		name = row.platform.strip()
		if not name:
			errors.append(f"Fila {number}: falta el nombre de la plataforma.")
			continue
		where = f"Fila {number} ({name})"
		if row.quota < 0:
			errors.append(f"{where}: el cupo no puede ser negativo.")
		if row.priority < 1:
			errors.append(f"{where}: la prioridad empieza en 1.")
		key = (name.lower(), row.room_type)
		if key in seen:
			first, first_name = seen[key]
			errors.append(f"{where}: {first_name} ya tiene cupo para {row.room_type} en la fila {first}.")
		else:
			seen[key] = (number, name)
	return errors


# ---------------------------------------------------------------- informe diario

DayPlan = tuple[str, date, tuple[Instruction, ...]]  # (tipo de habitación, fecha, instrucciones)
RangePlan = tuple[str, date, date, tuple[Instruction, ...]]  # (tipo, primera fecha, última fecha, instrucciones)


def group_ranges(rows: list[DayPlan]) -> list[RangePlan]:
	"""Junta los días SEGUIDOS de un mismo tipo con las mismas instrucciones, por tipo y fecha: el informe diario
	cabe en unas líneas en vez de una por noche."""
	ranges: list[RangePlan] = []
	for room_type, day, plan in sorted(rows, key=lambda r: (r[0], r[1])):
		if ranges:
			last_type, first, last, last_plan = ranges[-1]
			if last_type == room_type and last_plan == plan and (day - last).days == 1:
				ranges[-1] = (room_type, first, day, plan)
				continue
		ranges.append((room_type, day, day, plan))
	return ranges


def range_text(room_type: str, first: date, last: date, plan: tuple[Instruction, ...]) -> str:
	"""«Doble, del 26-12 al 27-12: Booking.com, abrir con 4 · Expedia, cerrar»."""
	when = f"el {_day(first)}" if first == last else f"del {_day(first)} al {_day(last)}"
	what = " · ".join(f"{i.platform}, {action_label(i).lower()}" for i in plan)
	return f"{room_type}, {when}: {what}"
