"""Cotizaciones del BCU: lectura de la respuesta SOAP guardada y tipos para la moneda de la casa (sin red, sin frappe)."""

from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree

import pytest

from kamra_uy.pure import bcu

FIXTURE = Path(__file__).parent / "fixtures" / "bcu-cotizaciones-2026-11-12.xml"
# la respuesta real del 24-11-2026 a las 16:42 de Montevideo (series 500, 1000, 1111, 2222, 2224 y 2225 del 19 al 24):
# del 24 todavía no hay nada
LIVE = Path(__file__).parent / "fixtures" / "bcu-cotizaciones-2026-11-24.xml"
NINE = Decimal("1e-9")
FRIDAY = date(2026, 11, 13)
# la respuesta del 12-11 solo trae el 2222: la carga de la caja se prueba con ese código
CODES = {**bcu.DEFAULT_CODES, "USD": 2222}


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
    assert bcu.DEFAULT_CODES == {"USD": 2225, "ARS": 500, "BRL": 1000, "EUR": 1111}


def test_dollar_of_the_previous_close(quotes) -> None:
    assert bcu.rates_for_home(quotes, "UYU", ["USD"], CODES, "TCC", FRIDAY)["USD"] == Decimal("40.589000000")


def test_dollar_of_the_day(quotes) -> None:
    rates = bcu.rates_for_home(quotes, "UYU", ["USD"], CODES, "TCC", FRIDAY, day_offset=0)
    assert rates["USD"] == Decimal("40.611000000")


def test_weekend_uses_the_last_quote_before(quotes) -> None:
    sunday = date(2026, 11, 15)
    assert bcu.rates_for_home(quotes, "UYU", ["USD"], CODES, "TCC", sunday)["USD"] == Decimal("40.611000000")


def test_euro_comes_from_its_number_not_its_iso_label(quotes) -> None:
    assert {q.code for q in quotes if q.code == 1111}  # el ISO de la respuesta dice «EURO», no «EUR»
    rates = bcu.rates_for_home(quotes, "UYU", ["EUR", "ARS", "BRL"], CODES, "TCC", FRIDAY)
    assert rates == {"EUR": Decimal("44.850845000"), "ARS": Decimal("0.026271000"), "BRL": Decimal("7.183258000")}


def test_cross_rates_for_a_dollar_home(quotes) -> None:
    rates = bcu.rates_for_home(quotes, "USD", ["UYU", "ARS", "USD"], CODES, "TCC", FRIDAY)
    assert rates["UYU"] == (Decimal(1) / Decimal("40.589")).quantize(NINE)
    assert rates["ARS"] == (Decimal("0.026271") / Decimal("40.589")).quantize(NINE)
    assert "USD" not in rates  # la casa no se cotiza contra sí misma


def test_currency_without_a_quote_is_left_out(quotes) -> None:
    rates = bcu.rates_for_home(quotes, "UYU", ["USD", "GBP"], CODES, "TCC", FRIDAY)
    assert set(rates) == {"USD"}
    early = bcu.rates_for_home(quotes, "UYU", ["USD"], CODES, "TCC", date(2026, 11, 12))
    assert early == {}  # nada en o antes del 11-11


def test_home_without_a_quote_gives_nothing(quotes) -> None:
    assert bcu.rates_for_home(quotes, "GBP", ["USD"], {**CODES, "GBP": 2700}, "TCC", FRIDAY) == {}
    assert bcu.rates_for_home(quotes, "CLP", ["USD"], CODES, "TCC", FRIDAY) == {}


def test_seller_side_and_configured_codes() -> None:
    quotes = bcu.parse_cotizaciones(_reply(data=_dato(code="2225", tcc="40.1", tcv="41.9")))
    rates = bcu.rates_for_home(quotes, "uyu", ["usd"], {"USD": 2225}, "tcv", FRIDAY)
    assert rates == {"USD": Decimal("41.900000000")}


def test_unknown_side_is_refused(quotes) -> None:
    with pytest.raises(ValueError):
        bcu.rates_for_home(quotes, "UYU", ["USD"], CODES, "MID", FRIDAY)


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


# ── la cotización de la factura (HR-348, D-HR-74 respuesta 7) ─────────────────────────────────────────────────────
TUESDAY = date(2026, 11, 24)


@pytest.fixture(scope="module")
def live() -> list[bcu.BcuQuote]:
    return bcu.parse_cotizaciones(LIVE.read_bytes())


def _at(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour))


def test_the_invoice_series_is_the_dollar_billete_buyer_side() -> None:
    # la regla 1: la serie 2225 es la que el BCU llama «DLS. USA BILLETE», y la factura toma su comprador (TCC)
    names = {}
    for dato in ElementTree.fromstring(LIVE.read_bytes()).iter():
        if dato.tag.endswith("datoscotizaciones.dato"):
            fields = {child.tag.rsplit("}", 1)[-1]: (child.text or "").strip() for child in dato}
            names[fields["Moneda"]] = fields["Nombre"]
    assert names["2225"] == "DLS. USA BILLETE"
    assert names["2222"] == "DOLAR USA"
    assert bcu.FISCAL_CODES == {"USD": 2225}
    assert bcu.FISCAL_SIDE == "TCC"


def test_the_invoice_takes_2225_and_its_buyer_side_not_another_dollar() -> None:
    data = (_dato("2026-11-23", "2222", "40.1", "40.2") + _dato("2026-11-23", "2224", "40.3", "40.4")
            + _dato("2026-11-23", "2225", "40.5", "41.9"))
    quotes = bcu.parse_cotizaciones(_reply(data=data))
    found = bcu.fiscal_quote(quotes, bcu.FISCAL_CODES["USD"], TUESDAY, _at(TUESDAY, 9))
    assert (found.code, found.tcc) == (2225, Decimal("40.5"))


def test_the_invoice_takes_the_previous_business_day_never_the_day_itself(live) -> None:
    # la regla 2: la del día anterior; con la del mismo día ya publicada, tampoco se usa esa
    found = bcu.fiscal_quote(live, 2225, date(2026, 11, 20), _at(date(2026, 11, 20), 9))
    assert (found.day, found.tcc) == (date(2026, 11, 19), Decimal("40.604"))
    assert bcu.fiscal_quote(live, 2225, TUESDAY, _at(TUESDAY, 0)).day == date(2026, 11, 23)


def test_after_a_weekend_the_invoice_takes_fridays_quote_at_any_hour(live) -> None:
    monday = date(2026, 11, 23)
    for on in (monday, date(2026, 11, 22), date(2026, 11, 21)):
        found = bcu.fiscal_quote(live, 2225, on, _at(on, 0))
        assert (found.day, found.tcc) == (date(2026, 11, 20), Decimal("40.629")), on


def test_a_weekday_without_a_quote_waits_until_noon_then_counts_as_a_holiday(live) -> None:
    # la regla 3: el miércoles 25 sin la del martes 24 (la respuesta es de antes de que el BCU la publique) no hay
    # cotización por la mañana; desde las 12, el 24 se toma por feriado y vale la del lunes
    wednesday = date(2026, 11, 25)
    assert bcu.fiscal_quote(live, 2225, wednesday, _at(wednesday, 11)) is None
    assert bcu.fiscal_quote(live, 2225, wednesday, _at(wednesday, bcu.SETTLED_HOUR)).day == date(2026, 11, 23)
    # una operación de días pasados ya no espera
    assert bcu.fiscal_quote(live, 2225, wednesday, _at(date(2026, 11, 30), 8)).day == date(2026, 11, 23)


def test_without_any_earlier_quote_there_is_no_invoice_rate(live) -> None:
    assert bcu.fiscal_quote(live, 2225, date(2026, 11, 19), _at(date(2026, 11, 19), 15)) is None
    assert bcu.fiscal_quote(live, 2700, TUESDAY, _at(TUESDAY, 15)) is None


def test_the_invoice_asks_the_bcu_up_to_the_day_before() -> None:
    assert bcu.fiscal_window(TUESDAY) == (date(2026, 11, 9), date(2026, 11, 23))
