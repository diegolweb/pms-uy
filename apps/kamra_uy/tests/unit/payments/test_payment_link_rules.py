"""Las reglas del enlace de pago que Kamra pide a core (HR-30), sin frappe."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from kamra_uy.pure.payments import PRE_ARRIVAL, link_expires_at, link_purpose, link_request

NOW = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
MVD = timezone(timedelta(hours=-3))


@pytest.mark.parametrize("status", PRE_ARRIVAL)
def test_before_arrival_without_charges_it_is_a_deposit(status) -> None:
    assert link_purpose(status, folio_has_charges=False) == "deposit"


def test_the_pre_arrival_states() -> None:
    assert PRE_ARRIVAL == ("Confirmed", "Pending Payment", "Held", "Waitlist")


# con cargos es el saldo, también de una cancelada con penalización o de un estado que no conoce esta regla
@pytest.mark.parametrize("status", [*PRE_ARRIVAL, "Checked In", "Checked Out", "Cancelled", "No Show", "In House"])
def test_with_charges_it_is_the_balance(status) -> None:
    assert link_purpose(status, folio_has_charges=True) == "balance"


def test_a_stay_in_house_is_the_balance_even_before_its_first_charge() -> None:
    assert link_purpose("Checked In", folio_has_charges=False) == "balance"


@pytest.mark.parametrize("status", [None, "", "Cancelled", "No Show"])
def test_without_a_live_reservation_it_is_other(status) -> None:
    assert link_purpose(status, folio_has_charges=False) == "other"


def test_without_a_reservation_charges_do_not_make_it_a_balance() -> None:
    assert link_purpose(None, folio_has_charges=True) == "other"


def test_without_a_hold_core_picks_the_expiry() -> None:
    assert link_expires_at(NOW, None) is None


@pytest.mark.parametrize("ahead", [timedelta(minutes=5), timedelta(hours=6), timedelta(days=30)])
def test_a_hold_in_range_is_the_expiry(ahead) -> None:
    assert link_expires_at(NOW, NOW + ahead) == NOW + ahead


@pytest.mark.parametrize("ahead", [timedelta(minutes=4, seconds=59), -timedelta(hours=1),
                                   timedelta(days=30, seconds=1)])
def test_a_hold_out_of_range_leaves_it_to_core(ahead) -> None:
    assert link_expires_at(NOW, NOW + ahead) is None


def _request(**over):
    args = dict(property_code="HR-TEST", folio="FOLIO-0001", reservation="RES-0001", amount=125.5,
                currency="USD", purpose="deposit", guest_name="Persona Inventada",
                guest_email="persona@example.com", return_url="https://hotel.example.com/pago",
                expires_at=datetime(2026, 12, 1, 18, 0, tzinfo=MVD))
    args.update(over)
    return link_request(**args)


def test_the_request_body() -> None:
    assert _request() == {
        "property": "HR-TEST",
        "folio": "FOLIO-0001",
        "reservation": "RES-0001",
        "amount": "125.50",
        "currency": "USD",
        "purpose": "deposit",
        "payer": {"name": "Persona Inventada", "email": "persona@example.com"},
        "return_url": "https://hotel.example.com/pago",
        "expires_at": "2026-12-01T21:00:00Z",
    }


def test_optional_parts_are_left_out() -> None:
    body = _request(reservation=None, guest_email="  ", expires_at=None)
    assert "reservation" not in body
    assert "expires_at" not in body
    assert body["payer"] == {"name": "Persona Inventada"}


def test_an_address_that_is_not_an_email_is_left_out() -> None:
    assert _request(guest_email="sin correo")["payer"] == {"name": "Persona Inventada"}


@pytest.mark.parametrize(("amount", "text"), [(100, "100.00"), (0.1 + 0.2, "0.30"), ("99.995", "100.00"),
                                              (1250.5, "1250.50")])
def test_the_amount_has_two_decimals(amount, text) -> None:
    assert _request(amount=amount)["amount"] == text


@pytest.mark.parametrize("amount", [0, -1, 0.004, "x"])
def test_an_amount_that_is_not_positive_is_refused(amount) -> None:
    with pytest.raises(ValueError):
        _request(amount=amount)


def test_the_currency_is_uyu_or_usd() -> None:
    assert _request(currency="uyu")["currency"] == "UYU"
    with pytest.raises(ValueError):
        _request(currency="EUR")
    with pytest.raises(ValueError):
        _request(currency=None)


def test_the_purpose_is_one_of_the_three() -> None:
    with pytest.raises(ValueError):
        _request(purpose="tip")


def test_a_payer_needs_a_name_and_long_names_are_cut() -> None:
    with pytest.raises(ValueError):
        _request(guest_name="  ")
    assert len(_request(guest_name="x" * 200)["payer"]["name"]) == 150


def test_a_naive_expiry_is_refused() -> None:
    with pytest.raises(ValueError):
        _request(expires_at=datetime(2026, 12, 1, 18, 0))
