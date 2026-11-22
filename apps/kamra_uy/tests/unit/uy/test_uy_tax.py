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


# --- el IVA con que se cotiza una noche (HR-239) ------------------------------------------------------------------
@pytest.mark.parametrize(("room_type_pct", "property_pct"), [(None, None), (None, 10), (12, 11), ("", 0)])
def test_a_price_without_iva_is_quoted_without_iva_for_everyone(room_type_pct, property_pct) -> None:
    # D-HR-40 y su aviso A6: la cotización en línea y el enlace de pago van sin IVA para todos; el de cada noche lo
    # decide core en el check-in (al extranjero, 0; al residente en temporada y al comprador con RUT, 10)
    assert uy_tax.quoted_room_rate(False, room_type_pct, property_pct) == Decimal("0")


@pytest.mark.parametrize(("room_type_pct", "property_pct", "expected"), [(None, None, "10"), (None, 11, "11"),
                                                                          (12, 11, "12"), (0, 0, "10")])
def test_a_price_with_iva_included_keeps_backing_it_out(room_type_pct, property_pct, expected) -> None:
    # con `rates_include_tax` el precio configurado lleva el IVA: se sigue sacando como hoy, así que el neto (y el
    # precio que Kamra manda al channel manager) no cambia
    assert uy_tax.quoted_room_rate(True, room_type_pct, property_pct) == Decimal(expected)


# --- el IVA de cada noche que se cobra al reservar (HR-266, D-HR-47) -------------------------------------------------
# Lo que factura core en el check-in (services/core/config/iva-rules.uy.yaml, primera regla que encaja): no residente con
# la copia del documento y el hotel en el MINTUR → 0 (también con RUT); consumidor fuera de la alta temporada → 0;
# residente → la tasa mínima (en la alta temporada, o con un comprador con RUT todo el año).
from datetime import date


@pytest.mark.parametrize(("value", "expected"), [("1", uy_tax.RESIDENT), (1, uy_tax.RESIDENT), (True, uy_tax.RESIDENT),
                                                 ("0", uy_tax.NON_RESIDENT), (0, uy_tax.NON_RESIDENT),
                                                 (False, uy_tax.NON_RESIDENT), (" 1 ", uy_tax.RESIDENT),
                                                 (uy_tax.RESIDENT, uy_tax.RESIDENT),
                                                 (uy_tax.NON_RESIDENT, uy_tax.NON_RESIDENT)])
def test_the_declared_residence_is_read_from_the_page_or_the_field(value, expected) -> None:
    assert uy_tax.declared_residence(value) == expected


@pytest.mark.parametrize("value", [None, "", " ", "2", "si", "sí", "yes", "true", "uruguay", "-1", 2, [], {}])
def test_anything_else_is_no_declaration(value) -> None:
    # sin valor por defecto que decida por el huésped: ni «sí» ni «no» supuestos
    assert uy_tax.declared_residence(value) is None


def _rates(declared, llegada, salida, rut=False, pct=None):
    return uy_tax.booking_room_rates(declared, llegada, salida, buyer_has_rut=rut, room_pct=pct)


def test_a_resident_pays_from_the_16th_of_november_not_on_the_15th() -> None:
    assert _rates(uy_tax.RESIDENT, date(2026, 11, 15), date(2026, 11, 17)) == {
        "2026-11-15": Decimal("0"), "2026-11-16": Decimal("10")}


def test_a_resident_pays_on_easter_sunday_not_on_the_monday_after() -> None:
    # 2027: Semana de Turismo del 22 al 28 de marzo; el lunes 29 ya es temporada baja (Decreto 267/001)
    assert _rates(uy_tax.RESIDENT, date(2027, 3, 28), date(2027, 3, 30)) == {
        "2027-03-28": Decimal("10"), "2027-03-29": Decimal("0")}


def test_a_stay_across_the_border_is_charged_night_by_night() -> None:
    rates = _rates(uy_tax.RESIDENT, "2026-11-13", "2026-11-19")
    assert [str(v) for v in rates.values()] == ["0", "0", "0", "10", "10", "10"]
    assert list(rates) == ["2026-11-13", "2026-11-14", "2026-11-15", "2026-11-16", "2026-11-17", "2026-11-18"]


def test_a_stay_across_new_year_stays_in_the_high_season() -> None:
    rates = _rates(uy_tax.RESIDENT, "2026-12-30", "2027-01-02")
    assert set(rates.values()) == {Decimal("10")}


@pytest.mark.parametrize(("llegada", "salida"), [("2026-11-13", "2026-11-19"), ("2027-01-04", "2027-01-09"),
                                                 ("2027-07-10", "2027-07-12")])
def test_a_non_resident_pays_no_iva_any_night(llegada, salida) -> None:
    assert set(_rates(uy_tax.NON_RESIDENT, llegada, salida).values()) == {Decimal("0")}


def test_a_non_resident_with_a_rut_still_pays_no_iva_as_core_bills_it() -> None:
    # core: «no_residente» va antes que las de residente y no mira el comprador
    assert set(_rates(uy_tax.NON_RESIDENT, "2027-07-10", "2027-07-12", rut=True).values()) == {Decimal("0")}


def test_a_resident_buyer_with_a_rut_pays_every_night_all_year() -> None:
    assert set(_rates(uy_tax.RESIDENT, "2027-07-10", "2027-07-13", rut=True).values()) == {Decimal("10")}
    assert set(_rates(uy_tax.RESIDENT, "2026-11-14", "2026-11-17", rut=True).values()) == {Decimal("10")}


def test_the_rate_is_the_room_rate_of_the_property() -> None:
    assert _rates(uy_tax.RESIDENT, "2026-12-01", "2026-12-02", pct=Decimal("11")) == {"2026-12-01": Decimal("11")}


@pytest.mark.parametrize("declared", [None, "", "1", "Uruguay"])
def test_without_a_declaration_there_is_no_rate_to_charge(declared) -> None:
    with pytest.raises(ValueError):
        _rates(declared, "2026-12-01", "2026-12-03")


def test_the_iva_of_the_nights_is_their_rate_times_the_night() -> None:
    # lo que Kamra suma a la reserva (fiscal.connector.apply_room_rates): importe de la noche × tasa / 100, por noche
    nights = {"2026-11-15": Decimal("230"), "2026-11-16": Decimal("230"), "2026-11-17": Decimal("199.99")}
    rates = _rates(uy_tax.RESIDENT, "2026-11-15", "2026-11-18")
    assert uy_tax.iva_of_nights(nights, rates) == Decimal("43.00")
    assert uy_tax.iva_of_nights(nights, _rates(uy_tax.NON_RESIDENT, "2026-11-15", "2026-11-18")) == Decimal("0.00")
