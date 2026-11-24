"""Las reglas del formulario de contacto de la web (HR-314), sin frappe.

La web del hotel manda a kamra_uy el nombre, el apellido, el email, el teléfono (opcional, con su código de país) y el
mensaje, el idioma de la página (`_lang`), el campo trampa (`sitio`) y cuánto tardó desde que se cargó la página
(`espera`, en ms). El servidor vuelve a validarlo todo: lo del navegador no vale nada.
"""

import pytest

from kamra_uy.pure.contact import (
    ERROR_CODES,
    FIELDS,
    LANGUAGES,
    MAX_LENGTH,
    MIN_WAIT_MS,
    check,
    clean_phone,
    is_email,
)

GOOD = {"nombre": "Ana", "apellido": "Pérez", "email": "ana@example.com", "telefono": "+598 99 123 456",
        "mensaje": "¿Tienen cochera?", "sitio": "", "espera": "5000", "_lang": "pt"}


def form(**over) -> dict:
    return {**GOOD, **over}


def test_a_good_form_passes_cleaned():
    r = check(form(nombre="  Ana  María ", apellido=" Pérez\tGómez ", mensaje="  Hola\r\n\r\n¿Tienen cochera?  "))
    assert r.ok and not r.spam and r.errors == {}
    assert r.data == {"nombre": "Ana María", "apellido": "Pérez Gómez", "email": "ana@example.com",
                      "telefono": "+598 99 123 456", "mensaje": "Hola\n\n¿Tienen cochera?", "idioma": "pt"}


def test_the_five_fields_in_the_order_of_the_page():
    assert FIELDS == ("nombre", "apellido", "email", "telefono", "mensaje")
    assert LANGUAGES == ("es", "pt", "en")


@pytest.mark.parametrize("field", ["nombre", "apellido", "email", "mensaje"])
def test_name_surname_email_and_message_are_required(field):
    for empty in ("", "   ", None):
        r = check(form(**{field: empty}))
        assert not r.ok and r.errors == {field: "requerido"}, empty


def test_the_phone_is_optional():
    for empty in ("", "  ", None):
        r = check(form(telefono=empty))
        assert r.ok and r.data["telefono"] == ""
    assert check({k: v for k, v in GOOD.items() if k != "telefono"}).ok


def test_every_error_at_once_with_its_code():
    r = check(form(nombre="", apellido="", email="ana", telefono="099 123 456", mensaje=""))
    assert not r.ok
    assert r.errors == {"nombre": "requerido", "apellido": "requerido", "email": "email", "telefono": "telefono",
                        "mensaje": "requerido"}
    assert set(r.errors.values()) <= set(ERROR_CODES)


@pytest.mark.parametrize("good", ["ana@example.com", "a.b+c@sub.example.org", "ANA@example.com", "o'brien@mail.example.org"])
def test_emails_that_pass(good):
    assert is_email(good)


@pytest.mark.parametrize("bad", ["ana", "ana@", "@example.com", "ana@example", "ana @example.com", "ana@exa mple.com",
                                 "a@b@example.com", "ana@example.com, otro@example.com", "ana@example.com\r\nBcc: x@y.z",
                                 "Ana <ana@example.com>", "ana;otro@example.com", "ana@.com", "ana@example..com",
                                 "ana..b@example.com", "ñandú@example.com"])
def test_emails_that_do_not_pass_and_nothing_that_could_add_a_recipient_or_a_header(bad):
    # ASCII: va en el Reply-To, y una dirección internacionalizada pediría SMTPUTF8 en todo el camino
    assert not is_email(bad)
    assert check(form(email=bad)).errors == {"email": "email"}


@pytest.mark.parametrize("raw, clean", [
    ("+598 99 123 456", "+598 99 123 456"),
    ("+55 (11) 91234-5678", "+55 (11) 91234-5678"),
    ("0059899123456", "+59899123456"),
    ("  +1 212.555.0100 ", "+1 212.555.0100"),
])
def test_a_phone_with_its_country_code(raw, clean):
    assert clean_phone(raw) == clean
    assert check(form(telefono=raw)).data["telefono"] == clean


@pytest.mark.parametrize("bad", ["099 123 456", "99123456", "+598", "+12345", "+598 99 123 456 789 0123",
                                 "+598 99 123 456 ext. 2", "+598-99-123-456\nBcc", "++598 99 123 456"])
def test_a_phone_without_country_code_or_with_other_things_does_not_pass(bad):
    assert clean_phone(bad) is None
    assert check(form(telefono=bad)).errors == {"telefono": "telefono"}


@pytest.mark.parametrize("field", ["nombre", "apellido", "email", "telefono", "mensaje"])
def test_each_field_has_its_maximum(field):
    top = MAX_LENGTH[field]
    long = {"email": "a" * (top - 11) + "@example.com", "telefono": "+5" + "9" * top}.get(field, "x" * (top + 1))
    assert len(long) > top
    assert check(form(**{field: long})).errors == {field: "largo"}


def test_the_maximum_counts_the_cleaned_text():
    assert check(form(nombre="  " + "x" * MAX_LENGTH["nombre"] + "  ")).ok


def test_control_characters_never_reach_the_mail():
    r = check(form(nombre="Ana\x00\x1b[31m", mensaje="Hola\x07 mundo​\tfin"))
    assert r.ok
    assert r.data["nombre"] == "Ana[31m"
    assert r.data["mensaje"] == "Hola mundo\tfin"


def test_the_language_of_the_page_or_spanish():
    for lang, expected in (("es", "es"), ("pt", "pt"), ("en", "en"), ("pt-BR", "pt"), ("EN", "en"), ("fr", "es"),
                           ("", "es"), (None, "es")):
        assert check(form(_lang=lang)).data["idioma"] == expected, lang


def test_the_trap_field_filled_is_spam_and_says_nothing_else():
    r = check(form(sitio="https://spam.example.com", email="no-es-un-email"))
    assert r.spam == "trampa" and not r.ok and r.errors == {}


@pytest.mark.parametrize("wait", ["0", "2999", "-1", "", "abc", None, "1e9x"])
def test_too_fast_or_without_a_time_is_refused(wait):
    assert MIN_WAIT_MS == 3000
    r = check(form(espera=wait))
    assert r.spam == "rapido" and not r.ok and r.errors == {}


def test_three_seconds_are_enough():
    assert check(form(espera="3000")).ok
    assert check(form(espera=3000.7)).ok


def test_the_browser_says_the_same_as_the_server():
    """La web adelanta los errores con las mismas reglas (web/src/lib/contacto-form.ts). La guarda vive aquí: la web no
    puede leer código AGPL (license-boundary), y kamra_uy sí puede leer la web."""
    import pathlib

    ts = (pathlib.Path(__file__).resolve().parents[5] / "web" / "src" / "lib" / "contacto-form.ts").read_text()
    assert "export const CAMPOS = [" + ", ".join(f"'{c}'" for c in FIELDS) + "] as const;" in ts
    assert "export const MAXIMO: Record<Campo, number> = { " + ", ".join(f"{k}: {v}" for k, v in MAX_LENGTH.items()) + " };" in ts
    assert f"export const ESPERA_MINIMA = {MIN_WAIT_MS};" in ts
