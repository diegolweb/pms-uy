"""Cotizaciones del BCU: lectura de la respuesta SOAP guardada y tipos para la moneda de la casa (sin red, sin frappe)."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree

import pytest

from kamra_uy.pure import bcu

FIXTURE = Path(__file__).parent / "fixtures" / "bcu-cotizaciones-2026-11-12.xml"
NINE = Decimal("1e-9")
FRIDAY = date(2026, 11, 13)


@pytest.fixture(scope="module")
def quotes() -> list[bcu.BcuQuote]:
    return bcu.parse_cotizaciones(FIXTURE.read_bytes())


def _reply(status: str = "1", data: str = "") -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/"><SOAP-ENV:Body>'
        '<wsbcucotizaciones.ExecuteResponse xmlns="Cotiza"><Salida>'
        f"<respuestastatus><status>{status}</status><codigoerror>0</codigoerror><mensaje>m</mensaje></respuestastatus>"
        f"<datoscotizaciones>{data}</datoscotizaciones>"
        "</Salida></wsbcucotizaciones.ExecuteResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>"
    ).encode()


def _dato(day: str = "2026-11-12", code: str = "2222", tcc: str = "40.5", tcv: str = "40.5") -> str:
    return (
        f"<datoscotizaciones.dato><Fecha>{day}</Fecha><Moneda>{code}</Moneda><CodigoISO>X</CodigoISO>"
        f"<TCC>{tcc}</TCC><TCV>{tcv}</TCV></datoscotizaciones.dato>"
    )


def test_parses_every_quote_of_the_saved_reply(quotes) -> None:
    assert len(quotes) == 8
    assert bcu.BcuQuote(date(2026, 11, 12), 2222, Decimal("40.589000"), Decimal("40.589000")) in quotes
    assert {q.code for q in quotes} == {500, 1000, 1111, 2222}


def test_default_codes() -> None:
    assert bcu.DEFAULT_CODES == {"USD": 2222, "ARS": 500, "BRL": 1000, "EUR": 1111}


def test_dollar_of_the_previous_close(quotes) -> None:
    assert bcu.rates_for_home(quotes, "UYU", ["USD"], bcu.DEFAULT_CODES, "TCC", FRIDAY)["USD"] == Decimal("40.589000000")


def test_dollar_of_the_day(quotes) -> None:
    rates = bcu.rates_for_home(quotes, "UYU", ["USD"], bcu.DEFAULT_CODES, "TCC", FRIDAY, day_offset=0)
    assert rates["USD"] == Decimal("40.611000000")


def test_weekend_uses_the_last_quote_before(quotes) -> None:
    sunday = date(2026, 11, 15)
    assert bcu.rates_for_home(quotes, "UYU", ["USD"], bcu.DEFAULT_CODES, "TCC", sunday)["USD"] == Decimal("40.611000000")


def test_euro_comes_from_its_number_not_its_iso_label(quotes) -> None:
    assert {q.code for q in quotes if q.code == 1111}  # el ISO de la respuesta dice «EURO», no «EUR»
    rates = bcu.rates_for_home(quotes, "UYU", ["EUR", "ARS", "BRL"], bcu.DEFAULT_CODES, "TCC", FRIDAY)
    assert rates == {"EUR": Decimal("44.850845000"), "ARS": Decimal("0.026271000"), "BRL": Decimal("7.183258000")}


def test_cross_rates_for_a_dollar_home(quotes) -> None:
    rates = bcu.rates_for_home(quotes, "USD", ["UYU", "ARS", "USD"], bcu.DEFAULT_CODES, "TCC", FRIDAY)
    assert rates["UYU"] == (Decimal(1) / Decimal("40.589")).quantize(NINE)
    assert rates["ARS"] == (Decimal("0.026271") / Decimal("40.589")).quantize(NINE)
    assert "USD" not in rates  # la casa no se cotiza contra sí misma


def test_currency_without_a_quote_is_left_out(quotes) -> None:
    rates = bcu.rates_for_home(quotes, "UYU", ["USD", "GBP"], bcu.DEFAULT_CODES, "TCC", FRIDAY)
    assert set(rates) == {"USD"}
    early = bcu.rates_for_home(quotes, "UYU", ["USD"], bcu.DEFAULT_CODES, "TCC", date(2026, 11, 12))
    assert early == {}  # nada en o antes del 11-11


def test_home_without_a_quote_gives_nothing(quotes) -> None:
    assert bcu.rates_for_home(quotes, "GBP", ["USD"], {**bcu.DEFAULT_CODES, "GBP": 2700}, "TCC", FRIDAY) == {}
    assert bcu.rates_for_home(quotes, "CLP", ["USD"], bcu.DEFAULT_CODES, "TCC", FRIDAY) == {}


def test_seller_side_and_configured_codes() -> None:
    quotes = bcu.parse_cotizaciones(_reply(data=_dato(code="2225", tcc="40.1", tcv="41.9")))
    rates = bcu.rates_for_home(quotes, "uyu", ["usd"], {"USD": 2225}, "tcv", FRIDAY)
    assert rates == {"USD": Decimal("41.900000000")}


def test_unknown_side_is_refused(quotes) -> None:
    with pytest.raises(ValueError):
        bcu.rates_for_home(quotes, "UYU", ["USD"], bcu.DEFAULT_CODES, "MID", FRIDAY)


def test_as_of_is_the_newest_quote_used(quotes) -> None:
    assert bcu.as_of(quotes, FRIDAY) == date(2026, 11, 12)
    assert bcu.as_of(quotes, FRIDAY, day_offset=0) == date(2026, 11, 13)
    assert bcu.as_of(quotes, date(2026, 11, 1)) is None


def test_status_other_than_one_is_an_error() -> None:
    with pytest.raises(ValueError, match="status"):
        bcu.parse_cotizaciones(_reply(status="0"))


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not xml",
        b"<a/>",
        b"<r><respuestastatus><status>1</status></respuestastatus></r>",
        b"<r><respuestastatus/></r>",
        b'<!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>',
        _reply(data=_dato(tcc="abc")),
        _reply(data=_dato(tcc="-1")),
        _reply(data=_dato(tcc="0")),
        _reply(data=_dato(tcc="NaN")),
        _reply(data=_dato(day="13/11/2026")),
        _reply(data=_dato(code="USD")),
        _reply(data="<datoscotizaciones.dato><Fecha>2026-11-12</Fecha></datoscotizaciones.dato>"),
    ],
)
def test_unexpected_xml_is_an_error(body) -> None:
    with pytest.raises(ValueError):
        bcu.parse_cotizaciones(body)


_UTF8_DECLARATION = '<?xml version="1.0" encoding="utf-8"?>'


def _valid_reply_with_dtd(codec: str, declared: str) -> bytes:
    """Una respuesta VÁLIDA (status 1, un dato) que además trae una DTD con una entidad interna, codificada con `codec`."""
    text = _reply(data=_dato(tcc="&tcc;")).decode()
    head = f'<?xml version="1.0" encoding="{declared}"?><!DOCTYPE SOAP-ENV:Envelope [<!ENTITY tcc "40.5">]>'
    return text.replace(_UTF8_DECLARATION, head, 1).encode(codec)


@pytest.mark.parametrize(
    ("codec", "declared", "message"),
    [
        ("utf-8", "utf-8", "DTD"),
        ("utf-16", "utf-16", "UTF-8"),  # con BOM
        ("utf-16-le", "utf-16", "UTF-8"),  # sin BOM: son bytes UTF-8 válidos, con NUL
        ("utf-16-be", "utf-16", "UTF-8"),
    ],
)
def test_a_valid_reply_carrying_a_dtd_is_rejected_in_any_encoding(codec, declared, message) -> None:
    body = _valid_reply_with_dtd(codec, declared)
    # Sin la guarda, ElementTree la leería y expandiría la entidad: el rechazo solo puede venir de la guarda.
    assert ElementTree.fromstring(body).find(".//{Cotiza}TCC").text == "40.5"
    with pytest.raises(ValueError, match=message):
        bcu.parse_cotizaciones(body)


def test_a_utf8_reply_with_a_bom_is_read() -> None:
    assert len(bcu.parse_cotizaciones(b"\xef\xbb\xbf" + _reply(data=_dato()))) == 1


def test_empty_list_of_quotes_is_valid() -> None:
    assert bcu.parse_cotizaciones(_reply()) == []


def test_request_asks_for_the_codes_and_the_window() -> None:
    body = bcu.request_body([2222, 1111], date(2026, 11, 3), FRIDAY)
    root = ElementTree.fromstring(body)
    ns = {"c": "Cotiza"}
    entrada = root.find(".//c:wsbcucotizaciones.Execute/c:Entrada", ns)
    assert entrada is not None
    assert [i.text for i in entrada.findall("c:Moneda/c:item", ns)] == ["2222", "1111"]
    assert entrada.findtext("c:FechaDesde", namespaces=ns) == "2026-11-03"
    assert entrada.findtext("c:FechaHasta", namespaces=ns) == "2026-11-13"
    assert entrada.findtext("c:Grupo", namespaces=ns) == "0"


def test_request_endpoint_constants() -> None:
    assert bcu.BCU_URL == "https://cotizaciones.bcu.gub.uy/wscotizaciones/servlet/awsbcucotizaciones"
    assert bcu.SOAP_ACTION == "Cotizaaction/AWSBCUCOTIZACIONES.Execute"
