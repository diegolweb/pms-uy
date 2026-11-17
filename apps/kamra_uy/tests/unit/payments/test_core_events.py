"""La lectura de los avisos firmados de core (HR-30), sin frappe."""

import json

import pytest

from kamra_uy.pure.payments import amount_to_float, parse_core_event, payment_mode

BASE = {"event_id": "6f1c9d36-1111-4222-8333-444444444444", "property": "HR-TEST", "folio": "FOLIO-0001",
        "payment_id": "7a2b0c11-1111-4222-8333-555555555555", "amount": "125.50", "currency": "USD"}
APPROVED = {**BASE, "type": "payment.approved", "link_id": "8c3d1e22-1111-4222-8333-666666666666",
            "card_country": None, "mode": "Payment Link", "paid_at": "2026-12-01T12:00:00Z"}
REFUNDED = {**BASE, "type": "payment.refunded", "link_id": APPROVED["link_id"],
            "refund_id": "9d4e2f33-1111-4222-8333-777777777777", "refunded_total": "125.50",
            "refunded_at": "2026-12-02T12:00:00Z"}
CHARGED_BACK = {**BASE, "type": "payment.charged_back", "link_id": APPROVED["link_id"],
                "charged_back_at": "2026-12-03T12:00:00Z"}


def _raw(event) -> bytes:
    return json.dumps(event).encode()


@pytest.mark.parametrize("event", [APPROVED, REFUNDED, CHARGED_BACK], ids=lambda e: e["type"])
def test_the_three_events_parse(event) -> None:
    assert parse_core_event(_raw(event)) == event


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b'"x"', b"\xff"])
def test_a_body_that_is_not_a_json_object_is_refused(raw) -> None:
    with pytest.raises(ValueError):
        parse_core_event(raw)


def test_another_event_type_is_refused() -> None:
    with pytest.raises(ValueError):
        parse_core_event(_raw({**APPROVED, "type": "cfe.accepted"}))


@pytest.mark.parametrize("field", ["event_id", "type", "property", "folio", "payment_id", "amount", "currency"])
def test_every_event_needs_its_common_fields(field) -> None:
    event = dict(APPROVED)
    del event[field]
    with pytest.raises(ValueError):
        parse_core_event(_raw(event))


@pytest.mark.parametrize(("event", "field"), [(APPROVED, "link_id"), (REFUNDED, "refund_id"),
                                              (REFUNDED, "refunded_total"), (CHARGED_BACK, "charged_back_at")])
def test_each_type_needs_its_own_fields(event, field) -> None:
    event = dict(event)
    del event[field]
    with pytest.raises(ValueError):
        parse_core_event(_raw(event))


@pytest.mark.parametrize("value", ["", "   ", 12, None])
def test_a_required_field_must_be_a_non_empty_string(value) -> None:
    with pytest.raises(ValueError):
        parse_core_event(_raw({**APPROVED, "folio": value}))


@pytest.mark.parametrize("amount", ["0", "0.00", "-1", "1.234", "1e3", "abc"])
def test_an_amount_that_is_not_positive_money_is_refused(amount) -> None:
    with pytest.raises(ValueError):
        parse_core_event(_raw({**APPROVED, "amount": amount}))


def test_an_unknown_currency_is_refused() -> None:
    with pytest.raises(ValueError):
        parse_core_event(_raw({**APPROVED, "currency": "EUR"}))


def test_the_mode_of_the_event_or_a_payment_link() -> None:
    assert payment_mode({**APPROVED, "mode": "Tarjeta extranjera"}) == "Tarjeta extranjera"
    assert payment_mode({**APPROVED, "mode": ""}) == "Payment Link"
    assert payment_mode({k: v for k, v in APPROVED.items() if k != "mode"}) == "Payment Link"
    assert payment_mode({**APPROVED, "mode": None}) == "Payment Link"


@pytest.mark.parametrize(("value", "number"), [("125.50", 125.5), ("1", 1.0), ("0.01", 0.01)])
def test_amount_to_float(value, number) -> None:
    assert amount_to_float(value) == number


@pytest.mark.parametrize("value", ["", "1,5", "-2", "1.234", None])
def test_amount_to_float_refuses_what_is_not_money(value) -> None:
    with pytest.raises(ValueError):
        amount_to_float(value)
