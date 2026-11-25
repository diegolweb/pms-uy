"""La seña perdida (HR-347, D-HR-74 pregunta 4), sin frappe: qué retiene el hotel, qué CFE o nota de crédito falta y
cómo va el comprobante (22 % dentro de lo retenido, e-Factura con RUT, el rechazo por falta de documento)."""

from decimal import Decimal

from kamra_uy.fiscal import forfeit, model

# lo que contesta determineIva a una línea `forfeit` (la regla es de core, HR-344)
SENA = {"rate": "22", "reason_code": "sena_retenida", "rule_id": "sena_retenida"}


def fee(amount="100", gst="10", name="fee1", description="Cancellation fee (Full Stay)", **extra) -> dict:
    amount_d = Decimal(amount)
    total = amount_d + amount_d * Decimal(gst) / 100
    return {"name": name, "charge_type": "Misc", "description": description, "amount": amount, "gst_rate": gst,
            "total": str(total), "posting_date": "2026-12-03", "auto_posted": 1, **extra}


def pay(amount, kind="Advance", name=None, **extra) -> dict:
    return {"name": name or f"p{amount}", "payment_kind": kind, "mode": "Tarjeta extranjera", "amount": amount,
            **extra}


def test_late_cancellation_fully_paid_retains_the_fee_with_its_taxes_and_bills_it_with_22_inside() -> None:
    # la cuenta: el cargo de 100 + 10 % de Kamra = 110; pagado por adelantado, 330
    keep = forfeit.retained([fee()], [pay("330")])
    assert keep == Decimal("110.00")
    steps = forfeit.plan(keep, [])
    assert steps == {"supplement": "110.00", "credits": []}
    line = forfeit.cfe_line("forfeit-1", "Seña", steps["supplement"], SENA)
    body = forfeit.cfe_request("hr", "F-1", "R-1/1", model.buyer("Ana", None), "USD", "40", [line], [], "v1")
    # al huésped no se le cobra más: el total del CFE es lo retenido y el 22 % va dentro
    assert body["prices_include_iva"] is True
    assert [(ln["unit_amount"], ln["rate"], ln["reason_code"]) for ln in body["lines"]] == [
        ("110.00", "22", "sena_retenida")]


def test_what_is_retained_is_at_most_what_was_paid() -> None:
    assert forfeit.retained([fee()], [pay("60")]) == Decimal("60.00")
    assert forfeit.retained([fee()], []) == Decimal("0.00")


def test_the_same_hook_twice_leaves_one_document() -> None:
    covered = [{"row": "UYCFE-1", "line_id": "forfeit-1", "rate": "22", "amount": "110.00"}]
    assert forfeit.plan(Decimal("110.00"), covered) == {"supplement": None, "credits": []}


def test_a_buyer_with_rut_gets_the_forfeit_as_an_e_factura() -> None:
    buyer = model.buyer("Empresa SA", None, rut="21-999999-0016")
    assert buyer["type"] == "rut"
    body = forfeit.cfe_request("hr", "F-1", "R-1/1", buyer, "USD", "40",
                               [forfeit.cfe_line("forfeit-1", "Seña", "110", SENA)], [], None)
    assert body["buyer"]["type"] == "rut" and body["prices_include_iva"] is True


def test_a_waived_fee_or_a_full_refund_before_billing_leaves_nothing() -> None:
    voided = [fee(), {**fee(name="void1", description="VOID: Cancellation fee - Gerente"), "amount": "-100",
                      "total": "-110", "reversal_of": "fee1"}]
    assert forfeit.retained(voided, [pay("330")]) == Decimal("0.00")
    refunded = [pay("330"), pay("-330", kind="Refund", name="r1")]
    assert forfeit.retained([fee()], refunded) == Decimal("0.00")
    assert forfeit.plan(Decimal("0.00"), []) == {"supplement": None, "credits": []}


def test_a_partial_refund_after_billing_is_a_credit_note_for_what_went_back() -> None:
    covered = [{"row": "UYCFE-1", "line_id": "forfeit-1", "rate": "22", "amount": "110.00"}]
    keep = forfeit.retained([fee()], [pay("110"), pay("-50", kind="Refund", name="r1")])
    assert keep == Decimal("60.00")
    assert forfeit.plan(keep, covered) == {"supplement": None, "credits": [
        {"row": "UYCFE-1", "lines": [{"line_id": "forfeit-1", "amount": "50.00"}]}]}


def test_the_no_show_fee_and_the_kept_credit_both_count_and_the_security_deposit_does_not() -> None:
    charges = [fee(description="No-show charge (First Night)"),
               fee(amount="200", gst="10", name="kept", description=forfeit.FORFEITED_ADVANCE),
               fee(name="room", description="Room night")]
    charges[2]["charge_type"] = "Room"
    payments = [pay("330"), pay("500", kind="Security Deposit", name="sd"),
                pay("-500", kind="Refund", name="sdr", refund_of="sd")]
    assert forfeit.retained(charges, payments) == Decimal("330.00")


def test_the_payments_sent_with_the_forfeit_add_up_to_what_is_retained() -> None:
    payments = [pay("80", name="a"), pay("-10", kind="Refund", name="r"), pay("300", name="b")]
    assert forfeit.capped_payments(payments, "110.00") == [
        {"mode": "Tarjeta extranjera", "amount": "80.00"}, {"mode": "Tarjeta extranjera", "amount": "30.00"}]


def test_over_the_cap_without_a_document_the_rejection_is_told_as_the_missing_document() -> None:
    error = ("validation_failed (422) — /body/buyer/doc_number: por el monto (o por llevar retenciones) el e-Ticket "
             "tiene que identificar al comprador")
    assert forfeit.needs_document(error)
    assert not forfeit.needs_document("validation_failed (422) — /body/lines: otra cosa")
    assert not forfeit.has_identity(model.buyer("Ana", None))
    assert forfeit.has_identity(model.buyer("Ana", {"doc_type": "passport", "country": "AR", "doc_number": "X1"}))
    assert forfeit.DOC_MISSING.startswith("Falta el documento del comprador")
