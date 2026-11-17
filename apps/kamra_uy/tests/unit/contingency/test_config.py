"""La validación de la configuración del modo contingencia (HR-34), sin frappe."""

from kamra_uy.pure.contingency import PlatformRow, config_errors


def _row(platform="Booking.com", room_type="RT-DOBLE", quota=4, priority=1) -> PlatformRow:
    return PlatformRow(platform, room_type, quota, priority)


def test_a_sane_configuration_has_no_errors() -> None:
    rows = [_row(), _row("Expedia", priority=2), _row(room_type="RT-SUITE", quota=1)]
    assert config_errors(0, 2, 120, rows) == []


def test_negative_numbers_are_refused() -> None:
    errors = config_errors(-1, -1, 120, [_row(quota=-2, priority=0)])
    assert errors == [
        "El colchón no puede ser negativo.",
        "El umbral de alerta no puede ser negativo.",
        "Fila 1 (Booking.com): el cupo no puede ser negativo.",
        "Fila 1 (Booking.com): la prioridad empieza en 1.",
    ]


def test_the_horizon_goes_from_one_day_to_a_year() -> None:
    assert config_errors(0, 2, 0, []) == ["El horizonte va de 1 a 365 días."]
    assert config_errors(0, 2, 366, []) == ["El horizonte va de 1 a 365 días."]
    assert config_errors(0, 2, 365, []) == []


def test_a_row_needs_a_platform_name() -> None:
    assert config_errors(0, 2, 120, [_row(platform="  ")]) == ["Fila 1: falta el nombre de la plataforma."]


def test_the_same_platform_twice_for_one_room_type_is_refused() -> None:
    rows = [_row(), _row(platform=" booking.com ", quota=2)]
    assert config_errors(0, 2, 120, rows) == ["Fila 2 (booking.com): Booking.com ya tiene cupo para RT-DOBLE en la fila 1."]
