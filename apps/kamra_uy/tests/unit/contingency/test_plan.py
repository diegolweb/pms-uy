"""La regla del modo contingencia (HR-34), sin frappe: qué abrir y qué cerrar en cada extranet.

Por tipo de habitación y fecha, disponibles = libres - colchón; las plataformas se recorren por prioridad y cada una se
abre con su cupo ENTERO si cabe en lo que queda (y lo resta); si no cabe, se cierra. Nunca un cupo partido.
"""

import pytest

from kamra_uy.pure.contingency import Instruction, PlatformQuota, changes, needs_alert, plan_day

Q = PlatformQuota


def _three(quota: int = 4) -> list[PlatformQuota]:
	return [Q("Booking.com", quota, 1), Q("Expedia", quota, 2), Q("Airbnb", quota, 3)]


def _summary(plan: list[Instruction]) -> list[tuple[str, str, int]]:
	return [(i.platform, i.action, i.rooms) for i in plan]


def test_three_free_with_quota_four_closes_every_platform() -> None:
	# la aceptación de la ficha: con cupo 4 en todas y 3 libres, se cierran todas
	assert _summary(plan_day(3, _three())) == [
		("Booking.com", "close", 0),
		("Expedia", "close", 0),
		("Airbnb", "close", 0),
	]


def test_ten_free_opens_the_first_two_whole_quotas() -> None:
	assert _summary(plan_day(10, _three())) == [
		("Booking.com", "open", 4),
		("Expedia", "open", 4),
		("Airbnb", "close", 0),
	]


def test_twelve_free_opens_all_three() -> None:
	assert _summary(plan_day(12, _three())) == [
		("Booking.com", "open", 4),
		("Expedia", "open", 4),
		("Airbnb", "open", 4),
	]


def test_buffer_is_taken_before_the_quotas() -> None:
	assert _summary(plan_day(10, _three(), buffer=2)) == [
		("Booking.com", "open", 4),
		("Expedia", "open", 4),
		("Airbnb", "close", 0),
	]
	assert _summary(plan_day(9, _three(), buffer=2)) == [
		("Booking.com", "open", 4),
		("Expedia", "close", 0),
		("Airbnb", "close", 0),
	]


def test_a_buffer_larger_than_the_free_rooms_closes_everything() -> None:
	assert [i.action for i in plan_day(3, _three(1), buffer=5)] == ["close", "close", "close"]


def test_no_free_rooms_closes_everything() -> None:
	assert [i.action for i in plan_day(0, _three())] == ["close", "close", "close"]


def test_a_zero_quota_is_always_closed() -> None:
	plan = plan_day(100, [Q("Booking.com", 0, 1), Q("Expedia", 4, 2)])
	assert _summary(plan) == [("Booking.com", "close", 0), ("Expedia", "open", 4)]


def test_a_quota_that_does_not_fit_does_not_stop_a_smaller_one_behind_it() -> None:
	# 5 libres: Booking (4) cabe, Expedia (4) ya no, Airbnb (1) sí: nunca se parte un cupo, pero se sigue recorriendo
	plan = plan_day(5, [Q("Booking.com", 4, 1), Q("Expedia", 4, 2), Q("Airbnb", 1, 3)])
	assert _summary(plan) == [("Booking.com", "open", 4), ("Expedia", "close", 0), ("Airbnb", "open", 1)]


def test_what_the_extranets_can_sell_never_exceeds_the_free_rooms() -> None:
	for free in range(0, 20):
		for buffer in range(0, 4):
			plan = plan_day(free, [Q("A", 3, 1), Q("B", 5, 2), Q("C", 2, 3), Q("D", 1, 4)], buffer)
			assert sum(i.rooms for i in plan) <= max(0, free - buffer)


def test_the_result_comes_in_priority_order() -> None:
	plan = plan_day(12, [Q("Airbnb", 4, 3), Q("Booking.com", 4, 1), Q("Expedia", 4, 2)])
	assert [i.platform for i in plan] == ["Booking.com", "Expedia", "Airbnb"]


def test_priority_ties_are_broken_by_name() -> None:
	plan = plan_day(4, [Q("Expedia", 4, 1), Q("Despegar", 4, 1)])
	assert _summary(plan) == [("Despegar", "open", 4), ("Expedia", "close", 0)]


def test_no_platforms_gives_an_empty_plan() -> None:
	assert plan_day(5, []) == []


@pytest.mark.parametrize(("free", "buffer"), [(-1, 0), (3, -1)])
def test_negative_free_or_buffer_is_refused(free: int, buffer: int) -> None:
	with pytest.raises(ValueError):
		plan_day(free, _three(), buffer)


def test_a_negative_quota_is_refused() -> None:
	with pytest.raises(ValueError):
		plan_day(5, [Q("Booking.com", -1, 1)])


def test_changes_lists_only_what_differs() -> None:
	k1 = ("2026-12-28", "Doble", "Booking.com")
	k2 = ("2026-12-28", "Doble", "Expedia")
	k3 = ("2026-12-29", "Doble", "Booking.com")
	before = {k1: Instruction("Booking.com", "open", 4), k2: Instruction("Expedia", "close", 0)}
	after = {
		k1: Instruction("Booking.com", "close", 0),
		k2: Instruction("Expedia", "close", 0),
		k3: Instruction("Booking.com", "open", 4),
	}
	assert changes(before, after) == [
		(k1, Instruction("Booking.com", "open", 4), Instruction("Booking.com", "close", 0)),
		(k3, None, Instruction("Booking.com", "open", 4)),
	]


def test_changes_is_empty_for_the_same_plan() -> None:
	plan = {("2026-12-28", "Doble", "Booking.com"): Instruction("Booking.com", "open", 4)}
	assert changes(plan, dict(plan)) == []


def test_changes_comes_sorted_by_key() -> None:
	after = {
		("2026-12-29", "Doble", "Expedia"): Instruction("Expedia", "close", 0),
		("2026-12-28", "Suite", "Airbnb"): Instruction("Airbnb", "close", 0),
		("2026-12-28", "Doble", "Booking.com"): Instruction("Booking.com", "close", 0),
	}
	assert [key for key, _, _ in changes({}, after)] == sorted(after)


def test_needs_alert_at_or_below_the_threshold() -> None:
	assert needs_alert(2, 2) is True
	assert needs_alert(0, 2) is True
	assert needs_alert(3, 2) is False
