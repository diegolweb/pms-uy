"""Las piezas puras del informe y los avisos del modo contingencia (HR-34): alias de plataforma, noches de una estancia,
el plan guardado en JSON, los textos para recepción y la huella de un aviso."""

from datetime import UTC, date

import pytest

from kamra_uy.pure.contingency import (
	Instruction,
	action_label,
	alert_text,
	horizon_text,
	instruction_text,
	match_platform,
	nights,
	notice_hash,
	parse_aliases,
	plan_from_json,
	plan_to_json,
	split_recipients,
)

DEC28 = date(2026, 12, 28)


def test_aliases_include_the_platform_name_lowercased_and_trimmed() -> None:
	assert parse_aliases("Booking.com", " Booking , BOOKING.COM,,booking com ") == frozenset(
		{"booking.com", "booking", "booking com"}
	)


def test_aliases_without_text_are_just_the_name() -> None:
	assert parse_aliases("Expedia", None) == frozenset({"expedia"})


def test_a_channel_matches_its_platform_without_case() -> None:
	aliases = {
		"Booking.com": parse_aliases("Booking.com", "Booking"),
		"Expedia": parse_aliases("Expedia", ""),
	}
	assert match_platform("booking", aliases) == "Booking.com"
	assert match_platform("  EXPEDIA ", aliases) == "Expedia"
	assert match_platform("Airbnb", aliases) is None
	assert match_platform(None, aliases) is None
	assert match_platform("", aliases) is None


def test_nights_of_a_stay_exclude_the_departure_day() -> None:
	assert nights(DEC28, date(2026, 12, 31)) == [DEC28, date(2026, 12, 29), date(2026, 12, 30)]


def test_a_day_use_holds_its_own_date() -> None:
	# como Kamra: GREATEST(check_out, check_in + 1)
	assert nights(DEC28, DEC28) == [DEC28]


def test_the_plan_round_trips_through_json() -> None:
	plan = {
		("2026-12-28", "Doble", "Booking.com"): Instruction("Booking.com", "open", 4),
		("2026-12-28", "Doble", "Expedia"): Instruction("Expedia", "close", 0),
	}
	stored = plan_to_json(plan)
	assert stored == {
		"2026-12-28|Doble|Booking.com": {"action": "open", "rooms": 4},
		"2026-12-28|Doble|Expedia": {"action": "close", "rooms": 0},
	}
	assert plan_from_json(stored) == plan


@pytest.mark.parametrize("stored", [None, "", {}, "not json"])
def test_an_empty_or_broken_stored_plan_is_empty(stored) -> None:
	assert plan_from_json(stored) == {}


def test_a_stored_plan_may_come_as_a_json_string() -> None:
	assert plan_from_json('{"2026-12-28|Doble|Airbnb": {"action": "close", "rooms": 0}}') == {
		("2026-12-28", "Doble", "Airbnb"): Instruction("Airbnb", "close", 0)
	}


def test_a_stored_plan_skips_malformed_entries() -> None:
	assert plan_from_json({"sin-separadores": {"action": "open", "rooms": 1}, "a|b|c": "x"}) == {}


def test_action_labels_for_the_page() -> None:
	assert action_label(Instruction("Booking.com", "open", 4)) == "Abrir con 4"
	assert action_label(Instruction("Booking.com", "close", 0)) == "Cerrar"


def test_instruction_texts_for_the_mail() -> None:
	assert instruction_text(Instruction("Booking.com", "close", 0), DEC28, "Doble", 3) == (
		"Cerrar Booking.com el 28-12 en Doble: quedan 3"
	)
	assert instruction_text(Instruction("Expedia", "open", 4), DEC28, "Doble", 9) == (
		"Abrir Expedia con 4 el 28-12 en Doble: quedan 9"
	)
	assert instruction_text(Instruction("Expedia", "close", 0), DEC28, "Doble", 1) == (
		"Cerrar Expedia el 28-12 en Doble: queda 1"
	)


def test_alert_text() -> None:
	assert alert_text(DEC28, "Doble", 2) == "Últimas habitaciones: quedan 2 en Doble el 28-12"
	assert alert_text(DEC28, "Doble", 1) == "Últimas habitaciones: queda 1 en Doble el 28-12"
	assert alert_text(DEC28, "Doble", 0) == "Sin habitaciones libres en Doble el 28-12"


def test_the_notice_hash_depends_on_content_not_order() -> None:
	a = notice_hash([("Cerrar Booking.com", 0), ("alerta", 1)])
	assert a == notice_hash([("alerta", 1), ("Cerrar Booking.com", 0)])
	assert a != notice_hash([("Cerrar Booking.com", 0)])
	assert len(a) == 64


def test_recipients_split_on_commas_semicolons_and_lines() -> None:
	assert split_recipients(" recepcion@hotel.test, gerencia@hotel.test;\nfundador@hotel.test,, ") == [
		"recepcion@hotel.test",
		"gerencia@hotel.test",
		"fundador@hotel.test",
	]
	assert split_recipients(None) == []


def test_the_daily_report_goes_at_seven_in_the_hotel_timezone() -> None:
	from datetime import datetime, timezone

	from kamra_uy.pure.contingency import is_daily_report_hour

	# Montevideo es UTC-3 todo el año: las 07:00 de allí son las 10:00 UTC
	assert is_daily_report_hour(datetime(2026, 12, 28, 10, 5, tzinfo=UTC), "America/Montevideo") is True
	assert is_daily_report_hour(datetime(2026, 12, 28, 7, 5, tzinfo=UTC), "America/Montevideo") is False
	# sin zona en la propiedad, la del hotel
	assert is_daily_report_hour(datetime(2026, 12, 28, 10, 59, tzinfo=UTC), None) is True
	# otra zona: Madrid en invierno es UTC+1
	assert is_daily_report_hour(datetime(2026, 12, 28, 6, 0, tzinfo=UTC), "Europe/Madrid") is True


def test_an_unknown_timezone_falls_back_to_the_hotel_one() -> None:
	from datetime import datetime, timezone

	from kamra_uy.pure.contingency import is_daily_report_hour

	assert is_daily_report_hour(datetime(2026, 12, 28, 10, 0, tzinfo=UTC), "Nowhere/Atlantis") is True


def test_consecutive_days_with_the_same_plan_become_one_range() -> None:
    from kamra_uy.pure.contingency import group_ranges, range_text

    open4 = (Instruction("Booking.com", "open", 4), Instruction("Expedia", "close", 0))
    closed = (Instruction("Booking.com", "close", 0), Instruction("Expedia", "close", 0))
    days = [date(2026, 12, 26), date(2026, 12, 27), date(2026, 12, 28), date(2026, 12, 29), date(2026, 12, 31)]
    rows = [
        ("Suite", days[0], open4),
        ("Doble", days[0], open4),
        ("Doble", days[1], open4),
        ("Doble", days[2], closed),
        ("Doble", days[3], open4),
        ("Doble", days[4], open4),  # el 30 falta: el rango se corta
    ]
    ranges = group_ranges(rows)
    assert ranges == [
        ("Doble", days[0], days[1], open4),
        ("Doble", days[2], days[2], closed),
        ("Doble", days[3], days[3], open4),
        ("Doble", days[4], days[4], open4),
        ("Suite", days[0], days[0], open4),
    ]
    assert range_text(*ranges[0]) == "Doble, del 26-12 al 27-12: Booking.com, abrir con 4 · Expedia, cerrar"
    assert range_text(*ranges[1]) == "Doble, el 28-12: Booking.com, cerrar · Expedia, cerrar"


def test_the_daily_report_heading_says_the_next_days_from_a_dd_mm_date() -> None:
	# El cambio del punto 24 (D-HR-30): la fecha en dd-mm como el resto del informe, nunca en ISO.
	text = horizon_text(date(2026, 12, 26), 120)
	assert text == "Lo que debe tener cada extranet en los próximos 120 días, desde el 26-12:"
	assert "2026" not in text
