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


def test_a_mintur_agency_pays_no_iva_off_season_only_while_its_constancia_is_valid() -> None:
    # HR-236 (D-HR-74, pregunta 11): la noche del vencimiento incluida; vencida o sin constancia, un RUT más
    def rate(night, until):
        return uy_tax.booking_room_rate(uy_tax.RESIDENT, night, buyer_has_rut=True, agency_until=until)

    assert rate(date(2027, 4, 5), date(2027, 4, 30)) == Decimal("0")
    assert rate(date(2027, 4, 30), date(2027, 4, 30)) == Decimal("0")
    assert rate(date(2027, 5, 5), date(2027, 4, 30)) == Decimal("10")
    assert rate(date(2027, 4, 5), None) == Decimal("10")
    # dentro de la alta temporada, el 10 % aunque la constancia valga
    assert rate(date(2027, 3, 27), date(2027, 4, 30)) == Decimal("10")


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


# --- HR-304: la cotización de recepción con la residencia, como la reserva que va a crear ---

def _quote(nightly, room_total=None, advance_pct=30):
    base = sum(n["rate"] for n in nightly) if room_total is None else room_total
    return {
        "nightly": [dict(n, gst_rate=0.0) for n in nightly],
        "room_total": float(base),
        "amount_before_tax": float(base),
        "tax_amount": 0.0,
        "tax_percent": 0.0,
        "amount_after_tax": float(base),
        "advance": {"percent": advance_pct, "season": None, "amount": round(base * advance_pct / 100, 2)},
        "totals": {"subtotal": float(base), "discount": 0.0, "tax": 0.0, "total": float(base), "deposit_required": 0},
        "line_items": [{"component": "accommodation", "amount": float(base), "tax_amount": 0.0, "tax_rate": 0.0}],
    }


def test_quote_nights_reparte_el_total_del_plan_como_la_reserva() -> None:
    nightly = [{"date": "2026-12-01", "rate": 100.0}, {"date": "2026-12-02", "rate": 100.0},
               {"date": "2026-12-03", "rate": 100.0}]
    # un plan de tarifa deja el total en 250: cada noche su parte y la última el redondeo (Reservation.quote_night_rows)
    assert uy_tax.quote_nights(nightly, 250) == {"2026-12-01": Decimal("83.33"), "2026-12-02": Decimal("83.33"),
                                                 "2026-12-03": Decimal("83.34")}
    assert uy_tax.quote_nights(nightly, 300) == {d: Decimal("100.00") for d in ("2026-12-01", "2026-12-02",
                                                                                  "2026-12-03")}
    assert uy_tax.quote_nights([], 0) == {}


def test_quote_with_rates_suma_el_iva_de_cada_noche_y_rehace_totales_y_sena() -> None:
    q = _quote([{"date": "2026-11-14", "rate": 100.0}, {"date": "2026-11-15", "rate": 100.0},
                {"date": "2026-11-16", "rate": 100.0}, {"date": "2026-11-17", "rate": 100.0}])
    rates = uy_tax.booking_room_rates(uy_tax.RESIDENT, "2026-11-14", "2026-11-18")
    out = uy_tax.quote_with_rates(q, rates)
    assert [n["gst_rate"] for n in out["nightly"]] == [0.0, 0.0, 10.0, 10.0]
    assert out["tax_amount"] == 20.0
    assert out["amount_after_tax"] == 420.0
    assert out["totals"]["tax"] == 20.0 and out["totals"]["total"] == 420.0
    assert out["advance"]["amount"] == 126.0
    assert out["tax_percent"] == 5.0
    assert out["line_items"][0]["tax_amount"] == 20.0 and out["line_items"][0]["tax_rate"] == 5.0
    assert q["tax_amount"] == 0.0  # no toca la cotización que le pasan


def test_quote_with_rates_del_extranjero_no_cambia_nada() -> None:
    q = _quote([{"date": "2026-12-01", "rate": 100.0}])
    out = uy_tax.quote_with_rates(q, uy_tax.booking_room_rates(uy_tax.NON_RESIDENT, "2026-12-01", "2026-12-02"))
    assert out["tax_amount"] == 0.0 and out["amount_after_tax"] == 100.0 and out["advance"]["amount"] == 30.0


def test_quote_with_rates_sin_sena_ni_lineas_ni_noches() -> None:
    q = _quote([{"date": "2026-12-01", "rate": 100.0}])
    for key in ("advance", "totals", "line_items"):
        q.pop(key)
    out = uy_tax.quote_with_rates(q, {"2026-12-01": Decimal("10")})
    assert out["amount_after_tax"] == 110.0 and "advance" not in out
    empty = uy_tax.quote_with_rates({"nightly": [], "room_total": 0, "amount_before_tax": 0, "tax_amount": 0,
                                     "amount_after_tax": 0}, {})
    assert empty["tax_percent"] == 0.0


# --- HR-377: con un vale, el IVA del residente va sobre el precio con descuento (como lo rebaja Kamra) ---

def test_discount_factor_es_lo_que_queda_tras_el_vale() -> None:
    assert uy_tax.discount_factor(480, 48) == Decimal("0.9")
    assert uy_tax.discount_factor(0, 0) == 1
    assert uy_tax.discount_factor(480, 0) == 1
    assert uy_tax.discount_factor(100, 150) == 0  # nunca menos de 0


@pytest.mark.parametrize(("discount", "tax", "total"), [(48, 43.20, 475.20), (0, 48.0, 528.0)])
def test_quote_with_rates_con_vale_cobra_el_iva_sobre_el_precio_con_descuento(discount, tax, total) -> None:
    # 3 noches × 160 de temporada, residente al 10 %; VERANO10 = 10 % → 480 − 48 = 432 sin IVA
    q = _quote([{"date": "2027-01-20", "rate": 160.0}, {"date": "2027-01-21", "rate": 160.0},
                {"date": "2027-01-22", "rate": 160.0}], advance_pct=100)
    q.update(meal_total=0.0, discount=float(discount), amount_before_tax=480.0 - discount)
    rates = {"2027-01-20": Decimal("10"), "2027-01-21": Decimal("10"), "2027-01-22": Decimal("10")}
    out = uy_tax.quote_with_rates(q, rates)
    assert out["tax_amount"] == tax
    assert out["amount_after_tax"] == total
    assert out["advance"]["amount"] == total
    assert out["line_items"][0]["tax_amount"] == tax
