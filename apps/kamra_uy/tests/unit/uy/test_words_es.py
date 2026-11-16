"""Importe en letras en español del Uruguay, como sale en el documento de cuenta (sin frappe)."""

from decimal import Decimal

import pytest

from kamra_uy.pure.words_es import amount_in_words, integer_words


def test_pesos_with_cents() -> None:
    assert amount_in_words(Decimal("1250.50"), "UYU") == "pesos uruguayos mil doscientos cincuenta con 50/100"


def test_dollars_with_cents() -> None:
    assert amount_in_words(Decimal("1250.50"), "USD") == "dólares estadounidenses mil doscientos cincuenta con 50/100"


def test_currency_code_is_case_insensitive() -> None:
    assert amount_in_words(Decimal("1"), "uyu") == "pesos uruguayos uno con 00/100"


@pytest.mark.parametrize(
    ("amount", "words"),
    [
        (0, "cero"),
        (1, "uno"),
        (15, "quince"),
        (16, "dieciséis"),
        (20, "veinte"),
        (21, "veintiuno"),
        (22, "veintidós"),
        (31, "treinta y uno"),
        (99, "noventa y nueve"),
        (100, "cien"),
        (101, "ciento uno"),
        (115, "ciento quince"),
        (500, "quinientos"),
        (777, "setecientos setenta y siete"),
        (1000, "mil"),
        (1001, "mil uno"),
        (2000, "dos mil"),
        (21000, "veintiún mil"),
        (31000, "treinta y un mil"),
        (100000, "cien mil"),
        (101000, "ciento un mil"),
        (999999, "novecientos noventa y nueve mil novecientos noventa y nueve"),
        (1_000_000, "un millón"),
        (1_000_001, "un millón uno"),
        (2_000_000, "dos millones"),
        (21_000_000, "veintiún millones"),
        (1_500_000, "un millón quinientos mil"),
        (1_000_000_000, "mil millones"),
        (1_000_000_000_000, "un billón"),
        (3_000_000_000_001, "tres billones uno"),
    ],
)
def test_integer_words(amount, words) -> None:
    assert integer_words(amount) == words


@pytest.mark.parametrize(
    ("amount", "tail"),
    [
        (Decimal("0"), "cero con 00/100"),
        (Decimal("1"), "uno con 00/100"),
        (Decimal("0.005"), "cero con 01/100"),
        (Decimal("0.004"), "cero con 00/100"),
        (Decimal("0.995"), "uno con 00/100"),
        (Decimal("21.07"), "veintiuno con 07/100"),
        (100, "cien con 00/100"),
        ("1000000", "un millón con 00/100"),
    ],
)
def test_cents_round_half_up(amount, tail) -> None:
    assert amount_in_words(amount, "UYU") == f"pesos uruguayos {tail}"


def test_unknown_currency_puts_its_code_in_front() -> None:
    assert amount_in_words(Decimal("2"), "brl") == "BRL dos con 00/100"


@pytest.mark.parametrize("bad", [Decimal("-0.01"), -1, "abc", Decimal("NaN"), Decimal("Infinity"), 10**18])
def test_refuses_what_it_cannot_spell(bad) -> None:
    with pytest.raises(ValueError):
        amount_in_words(bad, "UYU")
