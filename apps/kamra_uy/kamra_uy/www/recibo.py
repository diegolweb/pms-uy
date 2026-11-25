"""/recibo: el recibo del hotel de un cobro de la reserva (HR-349), para el huésped y para imprimir.

Lo enlaza el correo de confirmación (kamra_uy.receipts.issue.url): `?n=<recibo>&k=<clave>`. Sin la clave del recibo,
la página no dice nada de él (ni si existe). En el idioma con que se reservó. Sin JavaScript ni recursos externos.

Los textos están aprobados (D-HR-48, 22-11) y viven en kamra_uy/receipts/texts.py.
"""

from __future__ import annotations

no_cache = 1
sitemap = 0


def get_context(context):
    import frappe

    from kamra_uy.receipts import issue, model
    from kamra_uy.receipts.texts import TEXTOS

    params = frappe.form_dict or {}
    row = issue.by_key(params.get("n"), params.get("k"))
    context.no_cache = 1
    if not row:
        accept = frappe.request.headers.get("Accept-Language") if getattr(frappe, "request", None) else None
        lang = model.idioma((accept or "").split(",")[0])
        context.update({"lang": lang, "found": False, "title": TEXTOS[lang]["recibo"],
                        "message": TEXTOS[lang]["no_encontrado"]})
        return context
    v = model.view(row)
    context.update({"lang": v["lang"], "found": True, "title": f"{v['titulo']} {row.receipt_no} · {v['emisor']}",
                    "body": model.html(v)})
    return context
