"""La página /pago a la que Mercado Pago devuelve al huésped (HR-30): no confirma nada, solo dice en qué quedó, en su
idioma. Sin frappe: la parte que decide el mensaje."""

import pytest

from kamra_uy.www import pago


@pytest.mark.parametrize(("header", "lang"), [
    (None, "es"), ("", "es"), ("fr-FR,fr;q=0.9", "es"), ("pt-BR,pt;q=0.9,en;q=0.8", "pt"), ("en-GB", "en"),
    ("de;q=1, en;q=0.5, pt;q=0.7", "pt"), ("es-UY", "es"), ("EN-us", "en"), ("en;q=0, pt;q=0.1", "pt"),
    ("*", "es"), ("en;q=abc, pt", "pt"),
])
def test_the_language_follows_accept_language(header, lang) -> None:
    assert pago.language(header) == lang


@pytest.mark.parametrize(("status", "outcome"), [
    ("approved", "approved"), ("pending", "in_process"), ("in_process", "in_process"), ("authorized", "in_process"),
    ("in_mediation", "in_process"), ("rejected", "failed"), ("cancelled", "failed"), ("null", "failed"),
    ("", "failed"), (None, "failed"), ("whatever", "failed"),
])
def test_the_outcome_of_the_status(status, outcome) -> None:
    assert pago.outcome(status) == outcome


def test_an_approved_payment_names_the_property() -> None:
    view = pago.view({"collection_status": "approved"}, "es-UY", "Hotel de Prueba")
    assert view == {"lang": "es", "title": "Pago", "outcome": "approved",
                    "message": "¡Gracias! Recibimos tu pago. Hotel de Prueba te confirmará la reserva."}


def test_without_the_property_it_says_the_hotel() -> None:
    assert pago.view({"status": "approved"}, None, None)["message"] == (
        "¡Gracias! Recibimos tu pago. El hotel te confirmará la reserva.")


def test_collection_status_wins_over_status() -> None:
    assert pago.view({"collection_status": "rejected", "status": "approved"}, None, None)["outcome"] == "failed"


@pytest.mark.parametrize(("lang", "params", "text"), [
    ("es", {"status": "pending"}, "Tu pago está en proceso. Te avisaremos cuando se acredite."),
    ("es", {"status": "rejected"}, "El pago no se completó. Puedes volver a intentarlo desde el mismo enlace."),
    ("pt", {"status": "approved"}, "Obrigado! Recebemos o seu pagamento. Hotel X vai confirmar a sua reserva."),
    ("pt", {"status": "pending"}, "Seu pagamento está em processamento. Avisaremos você quando for creditado."),
    ("pt", {"status": "rejected"}, "O pagamento não foi concluído. Você pode tentar de novo pelo mesmo link."),
    ("en", {"status": "approved"}, "Thank you! We received your payment. Hotel X will confirm your booking."),
    ("en", {"status": "pending"}, "Your payment is being processed. We'll let you know once it clears."),
    ("en", {"status": "rejected"}, "The payment wasn't completed. You can try again from the same link."),
])
def test_the_three_messages_in_three_languages(lang, params, text) -> None:
    assert pago.view(params, lang, "Hotel X")["message"] == text


@pytest.mark.parametrize(("lang", "text"), [
    ("es", "Esto es un enlace de prueba: no se cobró nada."),
    ("pt", "Este é um link de teste: nada foi cobrado."),
    ("en", "This is a test link: nothing was charged."),
])
def test_a_demo_link_says_nothing_was_charged(lang, text) -> None:
    view = pago.view({"demo": "1", "status": "approved"}, lang, "Hotel X")
    assert (view["outcome"], view["message"]) == ("demo", text)


def test_every_language_has_every_text() -> None:
    keys = set(pago.TEXTS["es"])
    assert all(set(texts) == keys for texts in pago.TEXTS.values())
    assert set(pago.TEXTS) == {"es", "pt", "en"}


def test_the_page_is_never_cached_nor_listed() -> None:
    assert (pago.no_cache, pago.sitemap) == (1, 0)
