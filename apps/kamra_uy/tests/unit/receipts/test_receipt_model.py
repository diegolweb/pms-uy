"""El recibo del hotel al cobrar la reserva (HR-349, D-HR-74), sin frappe: qué línea de pago lo lleva, a qué pago
devuelve una devolución y qué dice. La numeración y el aviso repetido, contra la base: kamra_uy/receipts/tests."""

import ast
from datetime import date
from pathlib import Path

import pytest

from kamra_uy.receipts import model

PACKAGE = Path(__file__).resolve().parents[3] / "kamra_uy"


# ── qué línea lleva recibo ────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("status", ["Pending Payment", "Held", "Confirmed"])
@pytest.mark.parametrize("kind", ["Advance", "Payment"])
def test_money_in_before_arrival_gets_a_receipt(kind, status) -> None:
    assert model.kind_for(kind, 120.0, status) == model.RECEIPT


@pytest.mark.parametrize("status", ["Checked In", "Checked Out", "Cancelled", "No Show", None])
def test_money_in_after_arrival_or_without_a_live_booking_gets_none(status) -> None:
    # en casa, el dinero lo cubre la factura (D-HR-32); el pago tardío de una cancelada lo revisa Finanzas (HR-31)
    assert model.kind_for("Advance", 120.0, status) is None


def test_a_security_deposit_or_nothing_is_not_a_payment_of_the_booking() -> None:
    assert model.kind_for("Security Deposit", 120.0, "Confirmed") is None
    assert model.kind_for("Advance", 0, "Confirmed") is None


@pytest.mark.parametrize("status", ["Confirmed", "Cancelled", "No Show", "Pending Payment"])
def test_a_refund_before_check_in_or_of_a_cancelled_booking_gets_its_note(status) -> None:
    assert model.kind_for("Refund", -50.0, status) == model.REFUND


@pytest.mark.parametrize("status", ["Checked In", "Checked Out"])
def test_a_refund_after_check_in_goes_in_the_credit_note_not_here(status) -> None:
    assert model.kind_for("Refund", -50.0, status) is None


def test_a_refund_names_the_payment_it_gives_back() -> None:
    assert model.refunded_payment("row-123", "refund:pay-9:r-1") == ("row", "row-123")
    assert model.refunded_payment(None, "refund:pay-9:r-1") == ("reference", "pay-9")
    assert model.refunded_payment(None, "chargeback:pay-9") == ("reference", "pay-9")
    assert model.refunded_payment(None, "deposit:RES-1") is None
    assert model.refunded_payment(None, None) is None


# ── qué dice ──────────────────────────────────────────────────────────────────────────────────────────────────────
RECEIPT = {
    "kind": model.RECEIPT, "receipt_no": "R-7", "issued_on": date(2026, 12, 2), "issuer_name": "UT Hotel SA",
    "issuer_rut": "210000000019", "payer_name": "UT Ana Pérez", "reservation": "RES-0042",
    "check_in_date": date(2027, 1, 8), "check_out_date": date(2027, 1, 11), "amount": 390.0, "currency": "USD",
    "charged_amount": 15876.9, "charged_currency": "UYU", "language": "es",
}


def test_the_receipt_says_issuer_payer_date_amount_what_was_charged_in_pesos_and_the_stay() -> None:
    v = model.view(RECEIPT)
    rows = dict(v["filas"])
    assert v["titulo"] == "Recibo"
    assert rows == {
        "Número": "R-7", "Fecha": "02/12/2026", "Emisor": "UT Hotel SA", "RUT": "210000000019",
        "Recibimos de": "UT Ana Pérez",
        "Concepto": "Pago de la reserva RES-0042, estadía del 08/01/2027 al 11/01/2027",
        "Importe": "US$ 390", "Cobrado en pesos": "$ 15.876,90",
    }
    assert v["aviso"] == "Este recibo no es una factura. La factura se emite en el check-in."


def test_charged_in_the_link_currency_adds_nothing() -> None:
    rows = dict(model.view({**RECEIPT, "charged_amount": None, "charged_currency": None})["filas"])
    assert not any(k.startswith("Cobrado") for k in rows)
    assert model.charged("USD", 390, "USD") is None
    assert model.charged("UYU", 0, "USD") is None
    assert model.charged("uyu", -15876.9, "USD") == {"currency": "UYU", "amount": 15876.9}


def test_a_refund_note_has_the_amount_negative_and_names_the_receipt_it_refunds() -> None:
    v = model.view({**RECEIPT, "kind": model.REFUND, "receipt_no": "R-9", "amount": -390.0,
                    "charged_amount": -15876.9, "refund_of_no": "R-7"})
    rows = dict(v["filas"])
    assert v["titulo"] == "Constancia de devolución"
    assert rows["Importe"] == "US$ -390" and rows["Cobrado en pesos"] == "$ -15.876,90"
    assert rows["Devolvimos a"] == "UT Ana Pérez"
    assert rows["Concepto"] == ("Devolución del pago de la reserva RES-0042 (recibo R-7), estadía del 08/01/2027 al "
                                "11/01/2027")


@pytest.mark.parametrize("lang", ["pt", "en"])
def test_every_language_has_every_text_and_says_the_invoice_comes_at_check_in(lang) -> None:
    from kamra_uy.receipts.texts import TEXTOS

    assert set(TEXTOS[lang]) == set(TEXTOS["es"])
    v = model.view({**RECEIPT, "language": lang})
    assert "check-in" in v["aviso"] and "RES-0042" in dict(v["filas"])[TEXTOS[lang]["concepto"]]


def test_a_receipt_without_rut_or_payer_still_has_its_issuer() -> None:
    rows = dict(model.view({**RECEIPT, "issuer_rut": None, "payer_name": None})["filas"])
    assert "RUT" not in rows and "Recibimos de" not in rows and rows["Emisor"] == "UT Hotel SA"


def test_everything_from_the_booking_is_escaped() -> None:
    body = model.html(model.view({**RECEIPT, "payer_name": "<script>x</script>"}))
    assert "<script>" not in body and "&lt;script&gt;" in body


def test_numbers_and_names() -> None:
    assert model.number_label(12) == "R-12"
    assert model.doc_name("HRS", 12) == "HRS-R-12"


# ── el cableado ───────────────────────────────────────────────────────────────────────────────────────────────────
def _hooks() -> dict:
    values = {}
    for node in ast.parse((PACKAGE / "hooks.py").read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            values[node.targets[0].id] = ast.literal_eval(node.value)
    return values


def test_every_folio_save_is_hooked_and_the_doctype_is_scoped_by_property() -> None:
    hooks = _hooks()
    assert "kamra_uy.receipts.issue.on_folio_update" in hooks["doc_events"]["Folio"]["on_update"]
    assert hooks["permission_query_conditions"]["UY Receipt"] == "kamra.authz.property_query_conditions"
    assert hooks["has_permission"]["UY Receipt"] == "kamra.authz.property_has_permission"
    assert hooks["override_doctype_dashboards"]["Reservation"] == ["kamra_uy.receipts.issue.reservation_dashboard"]
    assert "Receipts" in (PACKAGE / "modules.txt").read_text().splitlines()
    for path in ("receipts/__init__.py", "receipts/doctype/__init__.py", "receipts/doctype/uy_receipt/__init__.py",
                 "www/recibo.py", "www/recibo.html"):
        assert (PACKAGE / path).is_file(), path


def test_nobody_creates_changes_or_deletes_a_receipt_and_one_payment_row_is_one_receipt() -> None:
    import json

    doc = json.loads((PACKAGE / "receipts" / "doctype" / "uy_receipt" / "uy_receipt.json").read_text())
    assert doc["name"] == "UY Receipt" and doc["module"] == "Receipts" and doc["in_create"] == 1
    assert not any(p.get(k) for p in doc["permissions"] for k in ("create", "write", "delete", "cancel", "amend"))
    fields = {f["fieldname"]: f for f in doc["fields"]}
    assert fields["payment_row"]["unique"] == 1
    assert all(f.get("read_only") for f in doc["fields"] if f["fieldtype"] not in ("Section Break", "Column Break"))
