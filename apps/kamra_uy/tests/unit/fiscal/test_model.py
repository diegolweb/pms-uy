"""La lógica del conector fiscal (HR-28), sin frappe: residencia y documento, petición al motor de IVA, conciliación
de lo facturado con los CFE ya emitidos, bloque impreso y aviso `cfe.updated`."""

import json
from decimal import Decimal

import pytest

from kamra_uy.fiscal import model, texts


# ── el documento (D-HR-74: manda el país del documento; HR-346) ──────────────────────────────────────────────────
def test_a_foreign_document_with_the_copy_goes_with_its_country_and_the_residence_only_as_data() -> None:
    guest = model.guest_facts("AR", "DNI", copy_on_file=True, residence="UY")
    assert guest == {"residence_country": "UY", "document": {"type": "dni", "country": "AR", "copy_on_file": True}}


def test_a_uruguayan_id_card_is_uruguayan_wherever_the_guest_lives() -> None:
    guest = model.guest_facts(None, "National ID", copy_on_file=True, residence="AR")
    assert guest["document"] == {"type": "ci", "country": "UY", "copy_on_file": True}


@pytest.mark.parametrize("country", [None, "", "  "])
def test_without_the_country_of_the_document_nothing_is_assumed(country) -> None:
    # HR-304 y HR-346: ni la residencia ni lo declarado dicen de dónde es un pasaporte
    with pytest.raises(model.ResidenceMissing):
        model.guest_facts(country, "Passport", copy_on_file=True, residence="UY")


@pytest.mark.parametrize(("declared", "country", "expected"), [
    ("No reside en Uruguay", "UY", "collect"),  # dijo documento extranjero y trae cédula: paga el IVA
    ("Reside en Uruguay", "AR", "refund"),  # dijo documento uruguayo y trae uno extranjero: se le devuelve
    ("Reside en Uruguay", "UY", None),
    ("No reside en Uruguay", "BR", None),
    (None, "UY", None),
])
def test_what_was_declared_against_the_document(declared, country, expected) -> None:
    assert model.declared_mismatch(declared, country) == expected


@pytest.mark.parametrize(("id_type", "country", "expected"), [
    ("National ID", "AR", ("ci", "UY")),  # la cédula de Uruguay es uruguaya, diga lo que diga recepción
    ("DNI", "AR", ("dni", "AR")),
    ("DNI", "ES", ("other", "ES")),  # el tipo DNI de la DGI solo vale para AR, BR, CL o PY
    ("RG", None, ("dni", "BR")),
    ("Passport", "US", ("passport", "US")),
    ("Other", "FR", ("other", "FR")),
    ("Passport", None, None),
])
def test_the_kamra_document_maps_to_the_dgi_document(id_type, country, expected) -> None:
    doc = model.document(id_type, "AB123", country)
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


def test_a_mintur_agency_rut_carries_its_constancia_date_and_nobody_else_does() -> None:
    # HR-236: solo un comprador con RUT lleva la constancia de agencia del MINTUR (core, buyerOf)
    guest = {"residence_country": "UY"}
    body = model.determine_request("P", NIGHTS, "UYU", guest, [], buyer_type="rut", mintur_agency_until="2027-04-30")
    assert body["buyer"] == {"type": "rut", "mintur_agency": {"constancia_valid_until": "2027-04-30"}}
    assert model.determine_request("P", NIGHTS, "UYU", guest, [], buyer_type="rut")["buyer"] == {"type": "rut"}
    body = model.determine_request("P", NIGHTS, "UYU", guest, [], buyer_type="consumer",
                                   mintur_agency_until="2027-04-30")
    assert body["buyer"] == {"type": "consumer"}


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
    assert model.type_codes("e-factura-exportacion") == [121, 221]
    assert model.type_codes("nc-e-factura-exportacion") == [122, 222]
    assert model.type_codes(None) == [101, 201, 102, 202, 111, 211, 112, 212, 121, 221, 122, 222]


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


# ── la moneda de la factura (HR-316, D-HR-57 y D-HR-60) ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(("check_in", "expected"), [
    ("2026-11-16", "USD"),  # empieza la alta temporada
    ("2026-12-20", "USD"),
    ("2027-03-28", "USD"),  # el domingo de Turismo de 2027, el último día de la alta temporada
    ("2027-03-29", "UYU"),
    ("2026-11-15", "UYU"),
    ("2026-07-10", "UYU"),
])
def test_the_desk_is_offered_dollars_in_high_season_and_pesos_the_rest_of_the_year(check_in, expected) -> None:
    assert model.proposed_currency(check_in) == expected


def test_a_reservation_in_dollars_converts_to_pesos_at_the_bcu_rate_and_back() -> None:
    # dólares → pesos: el importe por la cotización del dólar; pesos → dólares: dividido por ella
    assert model.conversion("40.123", "1") == Decimal("40.123")
    assert model.money(Decimal("100") * model.conversion("1", "40")) == "2.50"
    assert model.conversion("40.123", "40.123") == Decimal(1)


def test_converted_lines_keep_what_they_were_in_the_currency_of_the_reservation() -> None:
    lines = model.reconcile(_desired(), [])["supplement"]
    out = model.convert_lines(lines, model.conversion("40.123", "1"))
    assert [(ln["unit_amount"], ln["source_amount"]) for ln in out] == [("4012.30", "100.00"), ("4814.76", "120.00")]
    assert [ln["rate"] for ln in out] == ["0", "0"]
    # en la misma moneda no cambia nada: ni el importe ni las claves
    assert model.convert_lines(lines, Decimal(1)) == lines


def test_the_cfe_request_never_carries_the_amount_of_the_reservation() -> None:
    lines = model.convert_lines(model.reconcile(_desired(), [])["supplement"], Decimal("40"))
    body = model.cfe_request("HR-GATE-CFE", "FOLIO-1", "RES-1/1", model.buyer("Ana", None), "UYU", "1", lines,
                             [{"mode": "Efectivo", "amount": "8800.00"}], None)
    assert body["currency"] == "UYU" and body["fx_rate"] == "1"
    assert [ln["unit_amount"] for ln in body["lines"]] == ["4000.00", "4800.00"]
    assert all("source_amount" not in ln for ln in body["lines"])


def test_payments_go_in_the_currency_of_the_invoice() -> None:
    paid = [{"mode": "Tarjeta extranjera", "amount": "220.00"}, {"mode": "Efectivo", "amount": "-5"}]
    assert model.convert_payments(paid, Decimal("40.5")) == [{"mode": "Tarjeta extranjera", "amount": "8910.00"},
                                                              {"mode": "Efectivo", "amount": "-202.50"}]
    assert model.convert_payments(paid, Decimal(1)) == paid


def _converted_original(rate="40.123"):
    lines = model.convert_lines(model.reconcile(_desired(), [])["supplement"], Decimal(rate))
    return [{"row": "CFE-1", "lines": lines}]


def test_the_coverage_of_an_invoice_in_pesos_is_counted_in_the_currency_of_the_reservation() -> None:
    assert model.coverage(_converted_original(), []) == [
        {"row": "CFE-1", "line_id": "2026-12-20:Room", "rate": "0", "amount": "100.00", "invoice_amount": "4012.30"},
        {"row": "CFE-1", "line_id": "2026-12-21:Room", "rate": "0", "amount": "120.00", "invoice_amount": "4814.76"}]
    # lo que nada cubre de más: la misma estancia no pide ningún CFE ni ninguna nota de crédito
    assert model.reconcile(_desired(), model.coverage(_converted_original(), [])) == {"supplement": [], "credits": []}


def test_the_credit_note_of_an_invoice_in_pesos_goes_in_pesos() -> None:
    # salida anticipada: la noche entera que sobra vuelve tal como se facturó, al centavo
    plan = model.reconcile(_desired(nights=NIGHTS[:1]), model.coverage(_converted_original(), []))
    assert plan["credits"] == [{"row": "CFE-1", "lines": [
        {"line_id": "2026-12-21:Room", "amount": "4814.76", "source_amount": "120.00"}]}]
    # una parte de una noche, a la misma proporción con que se convirtió esa línea
    cheaper = [NIGHTS[0], {**NIGHTS[1], "amount": "90.00"}]
    plan = model.reconcile(_desired(nights=cheaper), model.coverage(_converted_original(), []))
    assert plan["credits"] == [{"row": "CFE-1", "lines": [
        {"line_id": "2026-12-21:Room", "amount": "1203.69", "source_amount": "30.00"}]}]


def test_what_a_credit_note_in_pesos_gave_back_is_out_of_the_coverage_in_both_currencies() -> None:
    credits = [{"references": "CFE-1", "lines": [
        {"line_id": "2026-12-21:Room", "amount": "1203.69", "source_amount": "30.00"}]}]
    assert model.coverage(_converted_original(), credits)[1] == {
        "row": "CFE-1", "line_id": "2026-12-21:Room", "rate": "0", "amount": "90.00", "invoice_amount": "3611.07"}
    credits.append({"references": "CFE-1", "lines": [
        {"line_id": "2026-12-21:Room", "amount": "3611.07", "source_amount": "90.00"}]})
    assert [c["line_id"] for c in model.coverage(_converted_original(), credits)] == ["2026-12-20:Room"]


def test_the_credit_note_sent_to_core_has_only_the_line_and_its_amount() -> None:
    assert model.credit_note_lines([{"line_id": "a", "amount": "1203.69", "source_amount": "30.00"},
                                    {"line_id": "b", "amount": "5.00"}]) == [
        {"line_id": "a", "amount": "1203.69"}, {"line_id": "b", "amount": "5.00"}]
    assert model.credit_note_lines(None) is None


def test_the_desk_sees_the_choice_with_the_proposal_and_the_rate() -> None:
    block = model.currency_choice("USD", "2026-07-10", "40.123", "2026-07-09")
    assert block["title"] == model.CURRENCY_TITLE
    assert [o["code"] for o in block["options"]] == ["USD", "UYU"]
    assert block["currency"] == "UYU"
    assert block["text"] == model.PROPOSED_UYU
    usd, uyu = block["options"]
    assert usd["label"] == "Dólares (USD)" and uyu["label"] == "Pesos uruguayos (UYU)"
    # la de pesos dice con qué cotización se convierte; la de dólares, el tipo de cambio que lleva el CFE
    assert uyu["note"] == model.CONVERTED_NOTE.format(source="USD", rate="40,123", day="09-07-2026")
    assert usd["note"] == model.FX_NOTE.format(currency="USD", rate="40,123", day="09-07-2026")
    assert model.currency_choice("USD", "2026-12-20", "40.123", "2026-12-19")["currency"] == "USD"
    assert model.currency_choice("USD", "2026-12-20", "40.123", "2026-12-19")["text"] == model.PROPOSED_USD


def test_without_a_rate_the_desk_is_told_to_load_it_before_invoicing() -> None:
    block = model.currency_choice("USD", "2026-12-20", None, None)
    assert {o["note"] for o in block["options"]} == {texts.FX_PENDING}


def test_a_hotel_in_pesos_converts_to_dollars_only_for_the_dollar_invoice() -> None:
    usd, uyu = model.currency_choice("UYU", "2026-12-20", "40", "2026-12-19")["options"]
    assert usd["note"] == model.CONVERTED_NOTE.format(source="UYU", rate="40", day="19-12-2026")
    assert uyu["note"] == ""


def test_the_rate_note_of_an_invoice_reads_like_the_desk_writes_it() -> None:
    assert model.fx_note("USD", "USD", "40.123", None) == model.FX_LINE.format(currency="USD", rate="40,123")
    assert model.fx_note("UYU", "USD", "1", "40.123") == model.CONVERTED_LINE.format(source="USD", rate="40,123")
    assert model.fx_note("UYU", "UYU", "1", None) == ""
    assert model.fx_note("USD", "", "39.5", None) == model.FX_LINE.format(currency="USD", rate="39,5")


# ── el anexo de huéspedes de la empresa (D-HR-74, pregunta 5; HR-346) ───────────────────────────────────────────────
RUT_BUYER = {"type": "rut", "name": "Empresa SA", "rut": "210000000017"}
EXPORT = {"line_id": "n1", "rate": "0", "reason_code": "no_residente", "tax_category": "export_and_assimilated",
          "quantity": "1", "unit_amount": "100.00", "description": "x"}
TAXED = {"line_id": "m1", "rate": "10", "reason_code": "residente_hospedaje", "quantity": "1", "unit_amount": "20.00",
         "description": "y"}


def test_the_annex_lists_foreign_documents_only() -> None:
    passport = model.document("Passport", "X1", "AR")
    assert model.annex_entry("Ana", None, passport) == {"name": "Ana", "nationality": "AR", "country": "AR",
                                                        "doc_type": "passport", "doc_number": "X1"}
    assert model.annex_entry("Ana", "BR", passport)["nationality"] == "BR"
    assert model.annex_entry("Juan", "UY", model.document("National ID", "12345678", None)) is None
    assert model.annex_entry("", "AR", passport) is None


def test_the_annex_goes_only_on_a_company_invoice_with_export_nights() -> None:
    annex = [model.annex_entry("Ana", "AR", model.document("Passport", "X1", "AR"))]
    body = model.cfe_request("P", "F", "R/1", RUT_BUYER, "USD", "40", [EXPORT, TAXED], [], None, annex=annex)
    assert body["guests_annex"] == annex
    assert "guests_annex" not in model.cfe_request("P", "F", "R/1", RUT_BUYER, "USD", "40", [TAXED], [], None,
                                                   annex=annex)
    consumer = {"type": "consumer", "name": "Ana"}
    assert "guests_annex" not in model.cfe_request("P", "F", "R/1", consumer, "USD", "40", [EXPORT], [], None,
                                                   annex=annex)


@pytest.mark.parametrize(("value", "expected"), [
    ("Uruguayan", "UY"), ("ARG", "AR"), ("D", "DE"), ("br", "BR"), ("Argentina", "AR"), ("Martian", None), ("", None),
])
def test_the_nationality_kamra_keeps_becomes_a_country(value, expected) -> None:
    assert model.nationality_code(value, "Uruguayan", {"BR"}, {"argentina": "AR"}) == expected


# ── el CFE que core pide partir (HR-345: 422 split_required; HR-346 manda las partes) ──────────────────────────────
PARTS = [{"type": "e-factura-exportacion", "line_ids": ["n1"]}, {"type": "e-ticket", "line_ids": ["m1"]}]


def test_only_a_split_required_error_has_parts() -> None:
    errors = [{"path": "/body/lines", "message": "x", "reason": "split_required", "parts": PARTS}]
    assert model.split_parts(errors) == PARTS
    assert model.split_parts([{"path": "/body/lines", "message": "un descuento…"}]) is None
    assert model.split_parts(None) is None


def test_each_part_takes_its_lines_and_its_share_of_the_payments() -> None:
    lines = [{**EXPORT, "source_amount": "100.00"}, {**TAXED, "source_amount": "20.00"}]
    body = model.cfe_request("P", "F", "R/1", {"type": "consumer", "name": "Ana"}, "USD", "40",
                             lines, [{"mode": "Tarjeta", "amount": "131.00"}], "v1")
    export, rest = model.split_request(body, lines, PARTS)
    assert [ln["line_id"] for ln in export["body"]["lines"]] == ["n1"]
    assert [ln["line_id"] for ln in rest["body"]["lines"]] == ["m1"]
    assert export["lines"][0]["source_amount"] == "100.00" and "source_amount" not in export["body"]["lines"][0]
    assert "tax_category" not in export["body"]["lines"][0]  # CfeLine no la admite: es de Kamra
    # 131 en proporción a los totales con IVA (100 y 22, de 122) y lo que queda: entre las dos, lo pagado al centavo
    assert export["body"]["payments"] == [{"mode": "Tarjeta", "amount": "107.38"}]
    assert rest["body"]["payments"] == [{"mode": "Tarjeta", "amount": "23.62"}]
    assert "invoice_number" not in export["body"] and export["body"]["rules_version"] == "v1"


@pytest.mark.parametrize("paid", [
    [("Tarjeta", "310.00")],
    [("Tarjeta", "93.00"), ("Efectivo", "217.00")],  # la seña por Mercado Pago y el saldo al llegar
    # con tres medios el reparto viejo daba 199,99 + 110,01 (58,06 + 6,45 + 135,48): el de 200, a crédito
    [("Tarjeta", "90.00"), ("Transferencia", "10.00"), ("Efectivo", "210.00")],
])
def test_a_stay_split_by_the_season_takes_on_each_cfe_its_total_with_iva(paid) -> None:
    # HR-317: 200 exentos + 100 al 10 % (110 con IVA), cobrados 310 → cada e-Ticket suma justo su total (formaPago
    # contado en Gsoft: lo pagado ≥ el total, sin tolerancia) y cada medio, lo cobrado con él
    exempt = {"line_id": "e1", "rate": "0", "reason_code": "hospedaje_fuera_temporada", "tax_category": "exempt",
              "quantity": "2", "unit_amount": "100.00", "description": "x"}
    taxed = {**TAXED, "unit_amount": "100.00"}
    body = model.cfe_request("P", "F", "R/1", {"type": "consumer", "name": "Ana"}, "UYU", "1", [exempt, taxed],
                             [{"mode": m, "amount": a} for m, a in paid], None)
    pieces = model.split_request(body, [exempt, taxed], [{"type": "e-ticket", "line_ids": ["e1"]},
                                                        {"type": "e-ticket", "line_ids": ["m1"]}])
    by_part = [sum(Decimal(p["amount"]) for p in piece["body"]["payments"]) for piece in pieces]
    assert by_part == [Decimal("200.00"), Decimal("110.00")]
    by_mode = {m: sum(Decimal(p["amount"]) for piece in pieces for p in piece["body"]["payments"] if p["mode"] == m)
               for m, _a in paid}
    assert by_mode == {m: Decimal(a) for m, a in paid}


def test_a_split_with_other_lines_than_the_ones_sent_is_refused() -> None:
    body = model.cfe_request("P", "F", "R/1", {"type": "consumer", "name": "Ana"}, "USD", "40", [EXPORT, TAXED], [],
                             None)
    with pytest.raises(ValueError):
        model.split_request(body, [EXPORT, TAXED], [PARTS[0], {"type": "e-ticket", "line_ids": ["zz"]}])


def test_the_annex_stays_only_with_the_part_that_has_the_export_nights() -> None:
    annex = [model.annex_entry("Ana", "AR", model.document("Passport", "X1", "AR"))]
    body = model.cfe_request("P", "F", "R/1", RUT_BUYER, "USD", "40", [EXPORT, TAXED], [], None, annex=annex)
    export, rest = model.split_request(body, [EXPORT, TAXED], [
        {"type": "e-factura-exportacion", "line_ids": ["n1"]}, {"type": "e-factura", "line_ids": ["m1"]}])
    assert export["body"]["guests_annex"] == annex and "guests_annex" not in rest["body"]
