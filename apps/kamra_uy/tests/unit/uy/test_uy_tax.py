"""Tasas de IVA del paquete uruguayo: valores por defecto y prioridad de la configuración (sin frappe)."""

from decimal import Decimal

import pytest

from kamra_uy.pure import uy_tax


def test_defaults_pending_the_accountant() -> None:
    assert uy_tax.DEFAULT_ROOM_VAT == Decimal("10")
    assert uy_tax.DEFAULT_STANDARD_VAT == Decimal("22")


@pytest.mark.parametrize("room_type_pct", [None, "", 0, "0", 0.0, Decimal("0")])
def test_room_rate_falls_back_to_the_property_when_the_room_type_has_none(room_type_pct) -> None:
    # Frappe guarda un Percent vacío como 0: un 0 del tipo de habitación no es una tasa elegida.
    assert uy_tax.room_rate(room_type_pct, 11) == Decimal("11")


@pytest.mark.parametrize("property_pct", [None, "", 0])
def test_room_rate_defaults_to_ten(property_pct) -> None:
    assert uy_tax.room_rate(None, property_pct) == Decimal("10")


@pytest.mark.parametrize("value", [12, 12.5, "12.50", Decimal("12")])
def test_room_type_rate_wins(value) -> None:
    assert uy_tax.room_rate(value, 11) == Decimal(str(value))


@pytest.mark.parametrize(("property_pct", "expected"), [(None, "22"), ("", "22"), (0, "22"), (18, "18"), ("22.5", "22.5")])
def test_standard_rate(property_pct, expected) -> None:
    assert uy_tax.standard_rate(property_pct) == Decimal(expected)


@pytest.mark.parametrize("bad", ["abc", -1, "-5", 101, float("nan"), float("inf")])
def test_nonsense_rates_are_refused(bad) -> None:
    with pytest.raises(ValueError):
        uy_tax.standard_rate(bad)
    with pytest.raises(ValueError):
        uy_tax.room_rate(bad, None)
