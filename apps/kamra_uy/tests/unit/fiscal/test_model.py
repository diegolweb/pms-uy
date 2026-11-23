"""La lógica del conector fiscal (HR-28), sin frappe: residencia y documento, petición al motor de IVA, conciliación
de lo facturado con los CFE ya emitidos, bloque impreso y aviso `cfe.updated`."""

import json
from decimal import Decimal

import pytest

from kamra_uy.fiscal import model, texts


# ── residencia y documento ────────────────────────────────────────────────────────────────────────────────────────
def test_a_foreign_resident_with_the_copy_goes_as_non_resident_with_document() -> None:
    guest = model.guest_facts("AR", "DNI", "30111222", copy_on_file=True)
    assert guest == {"residence_country": "AR", "document": {"type": "dni", "country": "AR", "copy_on_file": True}}


@pytest.mark.parametrize("residence", [None, "", "  "])
def test_without_residence_nothing_is_assumed(residence) -> None:
    # HR-304: antes se tomaba Uruguay (10 % al extranjero en alta temporada); ahora no se factura sin ella
    with pytest.raises(model.ResidenceMissing):
        model.guest_facts(residence, "Passport", "X123", copy_on_file=True)


@pytest.mark.parametrize(("country", "declared", "expected"), [
    ("AR", None, "AR"),
    ("ar ", "Reside en Uruguay", "AR"),  # el país del huésped (el del documento, en el check-in) manda
    (None, "Reside en Uruguay", "UY"),  # lo declaró él al reservar: no es suponer
    ("", "Reside en Uruguay", "UY"),
    (None, "No reside en Uruguay", None),  # sabemos que no reside, pero no dónde: el check-in pide el país
    (None, None, None),
    (None, "", None),
])
def test_invoice_residence(country, declared, expected) -> None:
    assert model.invoice_residence(country, declared) == expected


def test_a_guest_without_document_number_goes_without_document() -> None:
    guest = model.guest_facts("BR", "RG", "", copy_on_file=False)
    assert guest == {"residence_country": "BR"}


@pytest.mark.parametrize(("id_type", "residence", "expected"), [
    ("National ID", "AR", ("ci", "UY")),  # la cédula de Uruguay es uruguaya aunque se viva fuera
    ("DNI", "AR", ("dni", "AR")),
    ("DNI", "ES", None),  # el DNI solo vale para AR, BR, CL o PY
    ("RG", "BR", ("dni", "BR")),
    ("RG", "AR", ("dni", "BR")),
    ("Passport", "US", ("passport", "US")),
    ("Other", "FR", ("other", "FR")),
    ("Aadhaar", "IN", ("other", "IN")),
    ("Passport", None, None),
])
def test_the_kamra_document_maps_to_the_dgi_document(id_type, residence, expected) -> None:
    doc = model.document(id_type, "AB123", residence)
    assert (None if doc is None else (doc["doc_type"], doc["country"])) == expected


def test_the_document_number_is_trimmed_and_capped() -> None:
    assert model.document("Passport", "  X" + "9" * 60 + " ", "US")["doc_number"] == ("X" + "9" * 60)[:40]


@pytest.mark.parametrize(("charge_type", "kind"), [
    ("Room", "room"), ("Extra Guests", "room"), ("Discount", "room"), ("Early Check-in", "room"),
    ("Late Check-out", "room"), ("Meal Plan", "fnb"), ("F&B", "fnb"), ("Restaurant", "fnb"), ("Misc", "service"),
    (None, "service"),
])
def test_each_charge_type_has_its_kind(charge_type, kind) -> None:
    assert model.kind_for(charge_type) == kind


# ── importes ──────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("value", "text"), [(1250.5, "1250.50"), ("0.005", "0.01"), (-12, "-12.00"), (None, "0.00"),
                                             (Decimal("3.333"), "3.33")])
def test_money_has_two_decimals(value, text) -> None:
    assert model.money(value) == text


@pytest.mark.parametrize(("value", "text"), [(39.5, "39.5"), (0.025, "0.025"), (1, "1"), (39.1234567, "39.123457")])
def test_the_fx_rate_has_six_decimals_at_most(value, text) -> None:
    assert model.fx_text(value) == text


def test_a_zero_fx_rate_is_refused() -> None:
    with pytest.raises(ValueError):
        model.fx_text(0)


# ── motor de IVA ──────────────────────────────────────────────────────────────────────────────────────────────────
NIGHTS = [
    {"line_id": "2026-12-20:Room", "kind": "room", "date": "2026-12-20", "amount": "100.00",
     "description": "Alojamiento 20-12-2026"},
    {"line_id": "2026-12-21:Room", "kind": "room", "date": "2026-12-21", "amount": "120.00",
     "description": "Alojamiento 21-12-2026"},
]


def _determined(rate="0", reason="no_residente", rule="no_residente", ids=None):
    return {"rules_version": "2026-11-16.1", "lines": [
        {"line_id": i, "rate": rate, "reason_code": reason, "rule_id": rule, "legal_ref": "x",
         "pending_accountant": True, "tax_category": "exempt", "sources": []}
        for i in (ids or [n["line_id"] for n in NIGHTS])]}


def test_the_determine_request_carries_lines_guest_payments_and_buyer() -> None:
    guest = {"residence_country": "AR", "document": {"type": "dni", "country": "AR", "copy_on_file": True}}
    body = model.determine_request("HR-GATE-CFE", NIGHTS, "USD", guest,
                                   [{"mode": "Tarjeta extranjera", "amount": "220.00"}], buyer_type="consumer")
    assert body == {
        "property": "HR-GATE-CFE",
        "lines": [{"line_id": n["line_id"], "kind": "room", "amount": n["amount"], "currency": "USD",
                   "date": n["date"]} for n in NIGHTS],
        "guest": guest,
        "payments": [{"mode": "Tarjeta extranjera", "amount": "220.00", "currency": "USD"}],
        "billed_to": "guest_account",
        "buyer": {"type": "consumer"},
    }


def test_payments_that_are_not_positive_are_left_out() -> None:
    body = model.determine_request("P", NIGHTS, "UYU", {"residence_country": "UY"},
                                   [{"mode": "Cash", "amount": "0.00"}, {"mode": "Cash", "amount": "-5.00"}])
    assert "payments" not in body


def test_rates_merge_into_the_lines() -> None:
    lines = model.with_rates(NIGHTS, _determined())
    assert [(ln["line_id"], ln["rate"], ln["reason_code"], ln["rule_id"]) for ln in lines] == [
        ("2026-12-20:Room", "0", "no_residente", "no_residente"),
        ("2026-12-21:Room", "0", "no_residente", "no_residente")]
    assert lines[0]["pending_accountant"] is True


def test_a_line_the_engine_did_not_answer_is_an_error() -> None:
    with pytest.raises(ValueError):
        model.with_rates(NIGHTS, _determined(ids=["2026-12-20:Room"]))


def test_rate_by_date_reads_the_room_lines() -> None:
    lines = model.with_rates(NIGHTS, _determined("10", "residente_hospedaje", "residente_hospedaje"))
    assert model.room_rate_by_date(lines) == {"2026-12-20": Decimal("10"), "2026-12-21": Decimal("10")}


# ── conciliación ──────────────────────────────────────────────────────────────────────────────────────────────────
def _desired(rate="0", nights=NIGHTS):
    return model.with_rates(nights, _determined(rate, ids=[n["line_id"] for n in nights]))


def test_at_check_in_nothing_is_covered_and_the_whole_stay_goes_line_by_line() -> None:
    plan = model.reconcile(_desired(), [])
    assert plan["credits"] == []
    assert [(ln["line_id"], ln["unit_amount"], ln["rate"]) for ln in plan["supplement"]] == [
        ("2026-12-20:Room", "100.00", "0"), ("2026-12-21:Room", "120.00", "0")]
    assert all(ln["quantity"] == "1" for ln in plan["supplement"])


def _covered(rate="0"):
    return [{"row": "CFE-1", "line_id": n["line_id"], "rate": rate, "amount": n["amount"]} for n in NIGHTS]


def test_a_closing_that_matches_the_cfe_changes_nothing() -> None:
    assert model.reconcile(_desired(), _covered()) == {"supplement": [], "credits": []}


def test_an_extension_adds_one_adjustment_line_with_the_difference() -> None:
    third = {"line_id": "2026-12-22:Room", "kind": "room", "date": "2026-12-22", "amount": "80.00",
             "description": "Alojamiento 22-12-2026"}
    plan = model.reconcile(_desired(nights=[*NIGHTS, third]), _covered())
    assert plan["credits"] == []
    [line] = plan["supplement"]
    assert (line["unit_amount"], line["rate"], line["reason_code"], line["rule_id"]) == (
        "80.00", "0", "no_residente", "no_residente")
    assert line["line_id"].startswith("adj-0-")


def test_an_early_departure_credits_the_difference_against_the_last_lines() -> None:
    plan = model.reconcile(_desired(nights=NIGHTS[:1]), _covered())
    assert plan["supplement"] == []
    assert plan["credits"] == [{"row": "CFE-1", "lines": [{"line_id": "2026-12-21:Room", "amount": "120.00"}]}]


def test_a_credit_bigger_than_one_line_spreads_over_the_previous_ones() -> None:
    cheaper = [{**NIGHTS[0], "amount": "50.00"}]
    plan = model.reconcile(_desired(nights=cheaper), _covered())
    assert plan["credits"] == [{"row": "CFE-1", "lines": [
        {"line_id": "2026-12-21:Room", "amount": "120.00"}, {"line_id": "2026-12-20:Room", "amount": "50.00"}]}]


def test_a_voided_bill_credits_everything_that_is_still_covered() -> None:
    covered = [*_covered(), {"row": "CFE-2", "line_id": "adj-0-x", "rate": "0", "amount": "80.00"}]
    plan = model.reconcile([], covered)
    assert plan["supplement"] == []
    assert plan["credits"] == [
        {"row": "CFE-2", "lines": [{"line_id": "adj-0-x", "amount": "80.00"}]},
        {"row": "CFE-1", "lines": [{"line_id": "2026-12-21:Room", "amount": "120.00"},
                                   {"line_id": "2026-12-20:Room", "amount": "100.00"}]}]


def test_a_residence_fixed_after_check_in_credits_one_rate_and_bills_the_other() -> None:
    plan = model.reconcile(_desired("0"), _covered("10"))
    assert [ln["rate"] for ln in plan["supplement"]] == ["0", "0"]
    assert plan["credits"] == [{"row": "CFE-1", "lines": [{"line_id": "2026-12-21:Room", "amount": "120.00"},
                                                          {"line_id": "2026-12-20:Room", "amount": "100.00"}]}]


def test_cents_of_rounding_do_not_make_a_cfe() -> None:
    covered = _covered()
    covered[0]["amount"] = "100.004"
    assert model.reconcile(_desired(), covered) == {"supplement": [], "credits": []}


def test_coverage_subtracts_what_credit_notes_already_gave_back() -> None:
    originals = [{"row": "CFE-1", "lines": [{"line_id": n["line_id"], "rate": "0", "unit_amount": n["amount"],
                                             "quantity": "1"} for n in NIGHTS]}]
    credits = [{"references": "CFE-1", "lines": [{"line_id": "2026-12-21:Room", "amount": "20.00"}]},
               {"references": "CFE-1", "lines": None}]  # una NC sin líneas es total y cancela lo que quedaba
    assert model.coverage(originals, credits[:1]) == [
        {"row": "CFE-1", "line_id": "2026-12-20:Room", "rate": "0", "amount": "100.00"},
        {"row": "CFE-1", "line_id": "2026-12-21:Room", "rate": "0", "amount": "100.00"}]
    assert model.coverage(originals, credits) == []


def test_lines_fully_credited_disappear_from_coverage() -> None:
    originals = [{"row": "CFE-1", "lines": [{"line_id": "a", "rate": "10", "unit_amount": "50.00", "quantity": "2"}]}]
    assert model.coverage(originals, [{"references": "CFE-1", "lines": [{"line_id": "a", "amount": "100.00"}]}]) == []


# ── petición de CFE ───────────────────────────────────────────────────────────────────────────────────────────────
def test_the_cfe_request_has_the_business_key_buyer_and_rules_version() -> None:
    lines = model.reconcile(_desired(), [])["supplement"]
    buyer = model.buyer("Ana Pérez", model.document("DNI", "30111222", "AR"))
    body = model.cfe_request("HR-GATE-CFE", "FOLIO-1", "RES-1/1", buyer, "USD", "39.5", lines,
                             [{"mode": "Tarjeta extranjera", "amount": "220.00"}], "2026-11-16.1")
    assert body["invoice_number"] == "RES-1/1" and body["folio"] == "FOLIO-1"
    assert body["buyer"] == {"type": "consumer", "name": "Ana Pérez", "country": "AR", "doc_type": "dni",
                             "doc_number": "30111222"}
    assert body["fx_rate"] == "39.5" and body["rules_version"] == "2026-11-16.1"
    assert body["prices_include_iva"] is False
    assert body["payments"] == [{"mode": "Tarjeta extranjera", "amount": "220.00"}]
    assert {k for k in body["lines"][0]} == {"line_id", "description", "quantity", "unit_amount", "rate",
                                             "reason_code", "rule_id"}


def test_a_company_with_a_rut_gets_an_e_factura_buyer() -> None:
    assert model.buyer("Viajes SA", None, rut="21-999999-0016") == {"type": "rut", "name": "Viajes SA",
                                                                     "rut": "219999990016"}


def test_a_company_tax_id_that_is_not_a_rut_stays_a_consumer() -> None:
    assert model.buyer("Viajes SA", None, rut="30-71234567-9")["type"] == "consumer"


def test_the_buyer_name_is_capped_and_never_empty() -> None:
    assert model.buyer("", None)["name"] == "Consumidor final"
    assert len(model.buyer("x" * 300, None)["name"]) == 150


def test_descriptions_are_capped_for_the_dgi() -> None:
    long = [{**NIGHTS[0], "description": "y" * 400}]
    assert len(model.reconcile(_desired(nights=long), [])["supplement"][0]["description"]) == 250


def test_reference_numbers_are_stable() -> None:
    assert model.reference("RES-0001", 1) == "RES-0001/1"
    assert model.idempotency_key("site.test", "UYCFE-00001") == "kamra-uy-cfe:site.test:UYCFE-00001"


# ── aviso cfe.updated ─────────────────────────────────────────────────────────────────────────────────────────────
CFE = {"cfe_id": "5b0b3c1e-1111-4222-8333-444444444444", "status": "issued", "type": "e-ticket", "serie": "A",
       "number": "123", "cae": "90123456789", "qr_url": "https://www.dgi.gub.uy/ctdcfe?x=1",
       "security_code": "aB3dE5", "cae_range": {"from": "1", "to": "1000"}, "cae_expires_on": "2027-12-31",
       "created_at": "2026-12-20T15:00:00Z", "updated_at": "2026-12-20T15:00:05Z", "total": "220.00",
       "currency": "USD", "kind": "original", "issued_via": "provider", "dgi_status": "pending"}
EVENT = {"event_id": "6f1c9d36-1111-4222-8333-444444444444", "type": "cfe.updated", "property": "HR-GATE-CFE",
         "cfe": CFE}


def test_a_cfe_updated_event_parses() -> None:
    assert model.parse_cfe_event(json.dumps(EVENT).encode()) == EVENT


@pytest.mark.parametrize("raw", [b"", b"nope", b"[]", b"\xff"])
def test_a_body_that_is_not_an_object_is_refused(raw) -> None:
    with pytest.raises(ValueError):
        model.parse_cfe_event(raw)


@pytest.mark.parametrize("broken", [
    {**EVENT, "type": "payment.approved"}, {**EVENT, "event_id": ""}, {**EVENT, "property": 3},
    {**EVENT, "cfe": "x"}, {**EVENT, "cfe": {**CFE, "cfe_id": None}}, {**EVENT, "cfe": {**CFE, "status": "lost"}},
    {k: v for k, v in EVENT.items() if k != "cfe"},
])
def test_an_event_without_what_it_needs_is_refused(broken) -> None:
    with pytest.raises(ValueError):
        model.parse_cfe_event(json.dumps(broken).encode())


def test_the_fields_kamra_keeps_come_from_the_cfe() -> None:
    fields = model.cfe_fields(CFE)
    assert fields["core_status"] == "issued" and fields["serie"] == "A" and fields["number"] == "123"
    assert (fields["cae_from"], fields["cae_to"], fields["cae_expires_on"]) == ("1", "1000", "2027-12-31")
    assert fields["security_code"] == "aB3dE5" and fields["error"] == ""
    assert fields["core_updated_at"] == "2026-12-20T15:00:05Z"
    rejected = model.cfe_fields({**CFE, "status": "rejected", "error": {"code": "x", "message": "bad"}})
    assert rejected["error"] == "x: bad"


def test_an_older_update_does_not_overwrite_a_newer_one() -> None:
    assert model.is_newer("2026-12-20T15:00:05Z", "2026-12-20T15:00:01Z") is True
    assert model.is_newer("2026-12-20T15:00:01Z", "2026-12-20T15:00:05Z") is False
    assert model.is_newer("2026-12-20T15:00:05Z", "2026-12-20T15:00:05Z") is False
    assert model.is_newer("2026-12-20T15:00:05Z", None) is True
    assert model.is_newer("2026-12-20T15:00:05.250Z", "2026-12-20T15:00:05Z") is True


# ── bloque impreso ────────────────────────────────────────────────────────────────────────────────────────────────
def test_an_issued_cfe_prints_its_block() -> None:
    block = model.print_block(model.cfe_fields(CFE), "219999990016")
    assert block["kind"] == "uy_cfe"
    assert block["title"] == "e-Ticket"
    assert block["serie"] == "A" and block["number"] == "123"
    assert block["qr_url"] == CFE["qr_url"]
    assert block["security_code"] == "aB3dE5"
    assert block["cae"] == {"number": "90123456789", "from": "1", "to": "1000", "expires_on": "2027-12-31"}
    assert block["issuer_rut"] == "219999990016"
    assert block["verify_text"]
    assert block["contingency"] is False


def test_a_cfe_in_contingency_prints_that_it_is_pending() -> None:
    block = model.print_block({"core_status": "contingency", "cfe_type": "e-ticket"}, None)
    assert block["contingency"] is True and block["serie"] is None
    assert block["status_text"]


def test_a_credit_note_has_its_own_title() -> None:
    assert model.print_block({**model.cfe_fields(CFE), "cfe_type": "nc-e-ticket"}, None)["title"] == (
        "Nota de crédito de e-Ticket")


def test_lo_declarado_es_el_valor_del_campo_de_la_reserva() -> None:
    from kamra_uy.pure import uy_tax

    assert model.DECLARED_RESIDENT == uy_tax.RESIDENT


# ── HR-304: las facturas pendientes que recepción registra a mano (portal de Factura Express o papel) ──────────────
@pytest.mark.parametrize(("row", "expected"), [
    ({"kind": "Original", "status": "Sent", "core_status": "contingency"}, True),
    ({"kind": "Credit note", "status": "Sent", "core_status": "queued"}, True),
    ({"kind": "Original", "status": "Pending", "core_status": None}, True),  # aún no llegó a core
    ({"kind": "Original", "status": "Needs attention", "core_status": ""}, True),
    ({"kind": "Original", "status": "Sent", "core_status": "issued"}, False),
    ({"kind": "Original", "status": "Sent", "core_status": "rejected"}, False),
    ({"kind": "Reconcile", "status": "Pending", "core_status": None}, False),
])
def test_is_pending(row, expected) -> None:
    assert model.is_pending(row) is expected


def test_type_codes_are_the_electronic_and_the_contingency_one() -> None:
    assert model.type_codes("e-ticket") == [101, 201]
    assert model.type_codes("nc-e-factura") == [112, 212]
    assert model.type_codes(None) == [101, 201, 102, 202, 111, 211, 112, 212]


def test_manual_issue_builds_the_body_of_manual_issue() -> None:
    body = model.manual_issue("e-ticket", via="portal", type_code="201", serie=" a ", number="0001234",
                              issued_at="2026-12-07T10:30", note="  Portal de Factura Express ")
    assert body == {"via": "portal", "type_code": 201, "serie": "A", "number": "1234",
                    "issued_at": "2026-12-07T10:30:00-03:00", "note": "Portal de Factura Express"}
    assert model.manual_issue("e-factura", via="paper", type_code=111, serie="AB", number="7",
                              issued_at="2026-12-07T10:30:00-03:00")["issued_at"] == "2026-12-07T10:30:00-03:00"


@pytest.mark.parametrize(("fields", "fragment"), [
    ({"via": "email"}, "portal o papel"),
    ({"type_code": "111"}, "101 o 201"),
    ({"type_code": "x"}, "101 o 201"),
    ({"serie": ""}, "serie"),
    ({"serie": "A1"}, "serie"),
    ({"number": ""}, "número"),
    ({"number": "12a"}, "número"),
    ({"number": "12345678"}, "número"),
    ({"issued_at": "07/12/2026"}, "fecha"),
    ({"issued_at": ""}, "fecha"),
])
def test_manual_issue_rejects_what_core_would_not_take(fields, fragment) -> None:
    base = {"via": "portal", "type_code": "101", "serie": "A", "number": "1", "issued_at": "2026-12-07T10:30"}
    with pytest.raises(ValueError, match=fragment):
        model.manual_issue("e-ticket", **{**base, **fields})


# ── el CFE en el folio de recepción (HR-307; kamra.localization.folio_fiscal_documents) ─────────────────────────────
PAGE = "/facturas?property=Hotel%20Roosevelt"
ISSUED_ROW = {"kind": "Original", "status": "Sent", "core_status": "issued", "cfe_type": "e-ticket", "serie": "A",
              "number": "1234", "pdf_url": "https://example.org/cfe/A-1234.pdf",
              "issued_at": "2026-12-07T15:00:05-03:00"}


@pytest.mark.parametrize(("row", "state"), [
    ({"status": "Pending"}, "pending"),  # aún no llegó a core
    ({"status": "Sent", "core_status": "queued"}, "pending"),
    ({"status": "Sent", "core_status": "contingency"}, "contingency"),
    ({"status": "Sent", "core_status": "issued"}, "issued"),
    ({"status": "Sent", "core_status": "rejected"}, "attention"),
    ({"status": "Needs attention"}, "attention"),
    ({"status": "Sent", "core_status": "otro"}, "pending"),  # lo que no se conoce nunca es «emitido»
])
def test_desk_state(row, state) -> None:
    assert model.desk_state(row) == state


def test_nothing_asked_yet_is_no_card() -> None:
    assert model.desk_block([], [], PAGE) is None


def test_an_issued_cfe_shows_its_type_series_number_and_pdf_and_no_link() -> None:
    block = model.desk_block([ISSUED_ROW], [], PAGE)
    assert block == {
        "title": model.DESK_TITLE, "state": "issued", "text": "", "notice": "",
        "documents": [{"label": "e-Ticket", "state": "issued", "number": "Serie A, número 1234", "text": "",
                       "url": "https://example.org/cfe/A-1234.pdf", "date": "2026-12-07"}],
        "link": None,
    }


def test_a_cfe_in_contingency_has_no_number_and_leads_to_the_pending_invoices() -> None:
    row = {**ISSUED_ROW, "core_status": "contingency", "serie": None, "number": None, "pdf_url": None,
           "issued_at": None}
    block = model.desk_block([row], [], PAGE)
    assert block["state"] == "contingency"
    [doc] = block["documents"]
    assert (doc["number"], doc["url"], doc["date"]) == (None, None, None)
    assert doc["text"] == model.DESK_CONTINGENCY
    assert block["link"] == {"label": model.PENDING_PAGE, "url": PAGE}


def test_a_cfe_not_in_core_yet_says_it_is_retried_and_names_no_type_until_core_picks_it() -> None:
    block = model.desk_block([{"kind": "Original", "status": "Pending", "cfe_type": None}], [], PAGE)
    assert block["state"] == "pending"
    [doc] = block["documents"]
    assert doc["label"] == texts.TYPE_PENDING
    assert doc["text"] == model.STATUS_TEXTS["pending"]


def test_a_stay_whose_cfe_is_still_being_worked_out_is_pending() -> None:
    # la tarea del check-in aún no dio ningún CFE: no hay documentos, pero sí el estado
    block = model.desk_block([], [{"status": "Pending", "error": ""}], PAGE)
    assert (block["state"], block["documents"], block["notice"]) == ("pending", [], "")
    assert block["text"] == model.STATUS_TEXTS["pending"]
    assert block["link"] == {"label": model.PENDING_PAGE, "url": PAGE}


def test_without_the_residence_the_notice_says_what_to_do() -> None:
    block = model.desk_block([], [{"status": "Pending", "error": texts.RESIDENCE_MISSING}], PAGE)
    assert (block["state"], block["notice"], block["text"]) == ("pending", texts.RESIDENCE_MISSING, "")
    assert block["link"] == {"label": model.PENDING_PAGE, "url": PAGE}


def test_what_needs_finance_says_so_and_wins_over_the_rest() -> None:
    contingency = {**ISSUED_ROW, "core_status": "contingency"}
    rejected = {**ISSUED_ROW, "core_status": "rejected"}
    block = model.desk_block([ISSUED_ROW, contingency, rejected], [], PAGE)
    assert block["state"] == "attention"
    assert [d["state"] for d in block["documents"]] == ["issued", "contingency", "attention"]
    assert block["documents"][2]["text"] == model.STATUS_TEXTS["rejected"]
    attention = model.desk_block([{"kind": "Original", "status": "Needs attention", "cfe_type": "e-ticket"}], [],
                                 PAGE)
    assert attention["documents"][0]["text"] == model.NEEDS_ATTENTION
    assert model.desk_block([ISSUED_ROW], [{"status": "Needs attention", "error": "x"}], PAGE)["state"] == "attention"


def test_an_issued_stay_with_a_pending_adjustment_is_pending_not_issued() -> None:
    adjustment = {"kind": "Original", "status": "Pending", "cfe_type": "e-ticket"}
    assert model.desk_block([ISSUED_ROW, adjustment], [], PAGE)["state"] == "pending"
    assert model.desk_block([ISSUED_ROW, {**ISSUED_ROW, "cfe_type": "nc-e-ticket"}], [], PAGE)["documents"][1][
        "label"] == "Nota de crédito de e-Ticket"
