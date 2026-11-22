"""Las reglas del cobro con tarjeta y del botón del saldo que Kamra abre a la web (HR-222), sin frappe.

La web manda el token de la tarjeta que crea el formulario de Mercado Pago; kamra_uy arma el cuerpo de core
(`POST /v1/payments/links/{link_id}/card-payments`, contrato de HR-221) sin importe, con una clave de idempotencia
estable y devuelve códigos, no textos.
"""

import hashlib

import pytest

from kamra_uy.pure.payments import (
    CARD_CODES,
    amount_left,
    card_code,
    card_idempotency_key,
    card_payment_request,
    card_result,
    core_error_is_logged,
    wallet_idempotency_key,
)

LINK = "8c3d1e22-1111-4222-8333-666666666666"
TOK = "tok-card-ut-1"


def request(**over) -> dict:
    base = {"card_token": TOK, "payment_method_id": "visa", "issuer_id": "", "payer_email": "huesped@example.com",
            "identification_type": "", "identification_number": "", "device_id": ""}
    base.update(over)
    return card_payment_request(**base)


# --- lo que falta por pagar ----------------------------------------------------


def test_what_is_left_is_due_minus_paid_rounded_to_cents() -> None:
    assert amount_left(390, 0) == 390.0
    assert amount_left(390.004, 0) == 390.0
    assert amount_left("195.10", "0.05") == 195.05


def test_nothing_is_left_when_it_is_paid_or_paid_twice() -> None:
    assert amount_left(390, 390) == 0.0
    assert amount_left(390, 780) == -390.0
    assert amount_left(None, None) == 0.0


# --- la clave de idempotencia ----------------------------------------------------


def test_the_card_key_is_the_sha256_of_link_and_token() -> None:
    assert card_idempotency_key(LINK, TOK) == hashlib.sha256(f"{LINK}:{TOK}".encode()).hexdigest()


def test_the_same_token_on_the_same_link_gives_the_same_key() -> None:
    assert card_idempotency_key(LINK, TOK) == card_idempotency_key(LINK, TOK)
    assert card_idempotency_key(LINK, TOK) != card_idempotency_key(LINK, TOK + "x")
    assert card_idempotency_key(LINK, TOK) != card_idempotency_key("otro-enlace", TOK)


BACK_ES = "https://hotel.example.com/reservar/gracias?lang=es&booking=tok-estado"
BACK_PT = "https://hotel.example.com/reservar/gracias?lang=pt&booking=tok-estado"


def test_the_wallet_key_is_the_same_for_the_same_link_and_return() -> None:
    assert wallet_idempotency_key(LINK, BACK_ES) == wallet_idempotency_key(LINK, BACK_ES)
    assert wallet_idempotency_key(LINK, "") == wallet_idempotency_key(LINK, None)
    assert len(wallet_idempotency_key(LINK, BACK_ES)) == 64


def test_the_wallet_key_changes_with_the_body_it_goes_with() -> None:
    # HR-238: core compara el cuerpo con el de la clave; otra vuelta (otro idioma, o ninguna) es otro cuerpo,
    # así que es otra clave. Si no, core da 409 idempotency_conflict y el botón se muere para ese enlace.
    assert wallet_idempotency_key(LINK, BACK_ES) != wallet_idempotency_key(LINK, BACK_PT)
    assert wallet_idempotency_key(LINK, BACK_ES) != wallet_idempotency_key(LINK, "")
    assert wallet_idempotency_key(LINK, "") != wallet_idempotency_key("otro-enlace", "")
    assert wallet_idempotency_key(LINK, "") != card_idempotency_key(LINK, "wallet")


# --- el cuerpo para core ---------------------------------------------------------


def test_the_minimal_body_has_no_amount() -> None:
    assert request() == {"token": TOK, "payment_method_id": "visa", "payer": {"email": "huesped@example.com"}}


def test_the_full_body() -> None:
    body = request(issuer_id=" 310 ", identification_type="CI", identification_number="12345678",
                   device_id="armor.0123456789abcdef")
    assert body == {"token": TOK, "payment_method_id": "visa", "issuer_id": "310",
                    "payer": {"email": "huesped@example.com", "identification": {"type": "CI", "number": "12345678"}},
                    "device_id": "armor.0123456789abcdef"}
    assert not {"amount", "transaction_amount"} & set(body)


def test_an_identification_needs_both_type_and_number() -> None:
    assert "identification" not in request(identification_type="CI")["payer"]
    assert "identification" not in request(identification_number="12345678")["payer"]


@pytest.mark.parametrize("over", [
    {"card_token": ""}, {"card_token": "   "}, {"card_token": "x" * 257},
    {"payment_method_id": ""}, {"payment_method_id": "v" * 41},
    {"issuer_id": "9" * 41},
    {"payer_email": ""}, {"payer_email": "no-es-un-correo"}, {"payer_email": "a@" + "b" * 260 + ".com"},
    {"identification_type": "T" * 21, "identification_number": "1"},
    {"identification_type": "CI", "identification_number": "1" * 31},
    {"device_id": "d" * 201},
])
def test_what_core_would_refuse_is_refused_before_asking(over) -> None:
    with pytest.raises(ValueError):
        request(**over)


def test_the_error_never_carries_the_token_or_the_document() -> None:
    with pytest.raises(ValueError) as caught:
        request(card_token="SENTINEL-TOK" + "x" * 300, identification_type="CI", identification_number="99999999")
    assert "SENTINEL-TOK" not in str(caught.value)
    with pytest.raises(ValueError) as caught:
        request(identification_type="CI", identification_number="99999999" * 4)
    assert "99999999" not in str(caught.value)


# --- de un error de core a un código para la web -----------------------------------


def test_the_codes_the_web_translates() -> None:
    assert CARD_CODES == ("hold_expired", "link_not_payable", "card_not_enabled", "too_many_attempts", "unavailable",
                          "invalid")


@pytest.mark.parametrize("code", ["link_not_payable", "card_not_enabled", "too_many_attempts"])
def test_a_409_of_core_keeps_its_code(code) -> None:
    assert card_code(409, code) == code


@pytest.mark.parametrize("status,code", [
    (None, "upstream_unavailable"), (503, "upstream_unavailable"), (502, "currency_mismatch"), (500, "internal_error"),
    (409, "idempotency_in_progress"), (429, "http_429"),
])
def test_what_may_work_later_is_unavailable(status, code) -> None:
    assert card_code(status, code) == "unavailable"


@pytest.mark.parametrize("status,code", [
    (400, "validation_failed"), (422, "cc_rejected_bad_filled_security_code"), (404, "not_found"),
    (409, "idempotency_conflict"), (401, "unauthorized"), (403, "forbidden"),
])
def test_what_the_request_got_wrong_is_invalid(status, code) -> None:
    assert card_code(status, code) == "invalid"


@pytest.mark.parametrize(("status", "code"), [
    (409, "idempotency_conflict"),  # la misma clave con otro cuerpo: un fallo de Kamra, no del huésped (HR-238)
    (None, "upstream_unavailable"), (503, "upstream_unavailable"), (500, "internal_error"), (429, "http_429"),
    (409, "idempotency_in_progress"), (None, "not_configured"),
])
def test_what_reception_has_to_see_leaves_an_error_log(status, code) -> None:
    assert core_error_is_logged(status, code)


@pytest.mark.parametrize(("status", "code"), [
    (409, "link_not_payable"), (409, "card_not_enabled"), (409, "too_many_attempts"),
    (422, "cc_rejected_bad_filled_card_number"), (400, "validation_failed"),
])
def test_what_the_guest_can_fix_or_is_told_leaves_no_error_log(status, code) -> None:
    assert not core_error_is_logged(status, code)


def test_kamra_without_the_core_url_or_its_key_is_unavailable_not_no_card() -> None:
    # una configuración rota de Kamra (sin `hr_core_base_url`, o los ajustes sin clave) no es «esta casa no cobra con
    # tarjeta»: es un fallo que recepción tiene que ver en el Error Log, y el huésped, «inténtalo más tarde»
    assert card_code(None, "not_configured") == "unavailable"


# --- la respuesta de core, para la web ------------------------------------------------


def test_an_approved_payment() -> None:
    assert card_result({"payment_id": "p-1", "provider_payment_id": "123456789", "status": "approved",
                        "status_detail": "accredited", "three_ds": None}) \
        == {"status": "approved", "status_detail": "accredited", "payment_id": "p-1", "provider_payment_id": "123456789",
            "three_ds": None, "code": None}


def test_a_challenge_passes_through() -> None:
    three_ds = {"external_resource_url": "https://acs.example.com/challenge", "creq": "eyJjcmVxIjoiMSJ9"}
    out = card_result({"payment_id": "p-2", "status": "pending", "status_detail": "pending_challenge",
                       "three_ds": three_ds})
    assert out["three_ds"] == three_ds and out["status"] == "pending" and out["code"] is None


def test_nothing_else_of_core_reaches_the_web() -> None:
    out = card_result({"payment_id": "p-3", "status": "rejected", "status_detail": "cc_rejected_other_reason",
                       "three_ds": None, "payer": {"email": "x@example.com"}, "extra": 1})
    assert set(out) == {"status", "status_detail", "payment_id", "provider_payment_id", "three_ds", "code"}


def test_a_reply_without_a_known_status_is_unavailable() -> None:
    assert card_result({"payment_id": "p-4", "status": "weird"})["code"] == "unavailable"
    assert card_result({}) == {"status": "error", "status_detail": None, "payment_id": None, "provider_payment_id": None,
                               "three_ds": None, "code": "unavailable"}
