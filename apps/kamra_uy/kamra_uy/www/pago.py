"""/pago: la página a la que Mercado Pago devuelve al huésped tras pagar (HR-30).

No confirma nada por su cuenta (lo confirma el aviso firmado que llega aparte): solo dice en qué quedó el pago, en el
idioma del navegador (español, portugués de Brasil o inglés; español si no hay otro). Si `external_reference` es el
enlace de un folio, nombra la propiedad; nunca el huésped ni importes. Sin JavaScript ni recursos externos.

Los textos están aprobados (D-HR-48, 22-11: el fundador aprobó todos los textos).
"""

from __future__ import annotations

no_cache = 1
sitemap = 0

DEFAULT_LANGUAGE = "es"

TEXTS = {
    "es": {
        "title": "Pago",
        "approved": "¡Gracias! Recibimos tu pago. {property} te confirmará la reserva.",
        "approved_no_property": "¡Gracias! Recibimos tu pago. El hotel te confirmará la reserva.",
        "in_process": "Tu pago está en proceso. Te avisaremos cuando se acredite.",
        "failed": "El pago no se completó. Puedes volver a intentarlo desde el mismo enlace.",
        "demo": "Esto es un enlace de prueba: no se cobró nada.",
    },
    "pt": {
        "title": "Pagamento",
        "approved": "Obrigado! Recebemos o seu pagamento. {property} vai confirmar a sua reserva.",
        "approved_no_property": "Obrigado! Recebemos o seu pagamento. O hotel vai confirmar a sua reserva.",
        "in_process": "Seu pagamento está em processamento. Avisaremos você quando for creditado.",
        "failed": "O pagamento não foi concluído. Você pode tentar de novo pelo mesmo link.",
        "demo": "Este é um link de teste: nada foi cobrado.",
    },
    "en": {
        "title": "Payment",
        "approved": "Thank you! We received your payment. {property} will confirm your booking.",
        "approved_no_property": "Thank you! We received your payment. The hotel will confirm your booking.",
        "in_process": "Your payment is being processed. We'll let you know once it clears.",
        "failed": "The payment wasn't completed. You can try again from the same link.",
        "demo": "This is a test link: nothing was charged.",
    },
}

# los estados con los que vuelve Mercado Pago (collection_status o status) que aún pueden acabar acreditados
IN_PROCESS = ("pending", "in_process", "authorized", "in_mediation")


def language(accept_language: str | None) -> str:
    """El idioma de la página: el de más peso de Accept-Language entre es, pt y en; si no, español."""
    best, best_q = DEFAULT_LANGUAGE, 0.0
    for part in (accept_language or "").split(","):
        tag, _, params = part.strip().partition(";")
        code = tag.strip().lower().split("-")[0]
        if code not in TEXTS:
            continue
        q = 1.0
        for param in params.split(";"):
            key, _, value = param.strip().partition("=")
            if key == "q":
                try:
                    q = float(value)
                except ValueError:
                    q = 0.0
        if q > best_q:
            best, best_q = code, q
    return best


def outcome(status: str | None) -> str:
    if status == "approved":
        return "approved"
    if status in IN_PROCESS:
        return "in_process"
    return "failed"


def view(params: dict, accept_language: str | None, property_name: str | None) -> dict:
    lang = language(accept_language)
    texts = TEXTS[lang]
    if str(params.get("demo") or "") == "1":
        result, message = "demo", texts["demo"]
    else:
        result = outcome(params.get("collection_status") or params.get("status"))
        if result == "approved":
            message = texts["approved"].format(property=property_name) if property_name \
                else texts["approved_no_property"]
        else:
            message = texts[result]
    return {"lang": lang, "title": texts["title"], "outcome": result, "message": message}


def _property_name(external_reference: str | None) -> str | None:
    import frappe

    if not external_reference:
        return None
    prop = frappe.db.get_value("Folio", {"payment_link_id": str(external_reference)[:140]}, "property")
    if not prop:
        return None
    return frappe.db.get_value("Property", prop, "property_name") or prop


def get_context(context):
    import frappe

    params = dict(frappe.form_dict or {})
    accept = frappe.request.headers.get("Accept-Language") if getattr(frappe, "request", None) else None
    context.no_cache = 1
    context.update(view(params, accept, _property_name(params.get("external_reference"))))
