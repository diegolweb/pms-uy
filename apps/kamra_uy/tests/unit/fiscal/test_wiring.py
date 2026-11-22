"""El cableado del conector fiscal (HR-28), leído sin ejecutar nada: ganchos de hooks.py, módulo «Fiscal» y doctype."""

import ast
import json
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[3] / "kamra_uy"


def _hooks() -> dict:
    values = {}
    for node in ast.parse((PACKAGE / "hooks.py").read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            values[node.targets[0].id] = ast.literal_eval(node.value)
    return values


def _defined(dotted: str) -> bool:
    module, _, func = dotted.rpartition(".")
    source = PACKAGE.parent / (module.replace(".", "/") + ".py")
    tree = ast.parse(source.read_text())
    return any(isinstance(n, ast.FunctionDef) and n.name == func for n in tree.body)


def _flat(value) -> list[str]:
    return value if isinstance(value, list) else [value]


def test_check_in_closing_voiding_and_guest_changes_are_hooked() -> None:
    events = _hooks()["doc_events"]
    assert "kamra_uy.fiscal.events.on_reservation_validate" in _flat(events["Reservation"]["validate"])
    assert "kamra_uy.fiscal.events.on_reservation_update" in _flat(events["Reservation"]["on_update"])
    assert _flat(events["Folio"]["on_update"]) == ["kamra_uy.fiscal.events.on_folio_update"]
    assert _flat(events["Cancelled Invoice"]["after_insert"]) == ["kamra_uy.fiscal.events.on_invoice_cancelled"]
    assert _flat(events["Guest"]["on_update"]) == ["kamra_uy.fiscal.events.on_guest_update"]


def test_the_sweep_has_who_starts_it() -> None:
    assert _hooks()["scheduler_events"]["cron"]["*/5 * * * *"] == ["kamra_uy.fiscal.sync.sweep"]


def test_every_hooked_handler_exists() -> None:
    hooks = _hooks()
    handlers = [h for dt in hooks["doc_events"].values() for v in dt.values() for h in _flat(v)]
    handlers += [h for jobs in hooks["scheduler_events"]["cron"].values() for h in jobs]
    missing = [h for h in handlers if not _defined(h)]
    assert missing == []


def test_the_uy_cfe_doctype_lives_in_the_fiscal_module() -> None:
    doc = json.loads((PACKAGE / "fiscal" / "doctype" / "uy_cfe" / "uy_cfe.json").read_text())
    assert doc["name"] == "UY CFE" and doc["module"] == "Fiscal"
    assert "Fiscal" in (PACKAGE / "modules.txt").read_text().splitlines()
    for path in ("fiscal/__init__.py", "fiscal/doctype/__init__.py", "fiscal/doctype/uy_cfe/__init__.py"):
        assert (PACKAGE / path).is_file(), path
    fields = {f["fieldname"]: f for f in doc["fields"]}
    assert fields["core_cfe_id"]["unique"] == 1
    assert set(fields["status"]["options"].split("\n")) == {"Pending", "Sent", "Done", "Not needed", "Needs attention"}
    assert all(f.get("read_only") for f in doc["fields"] if f["fieldtype"] not in ("Section Break", "Column Break"))


def test_the_webhook_route_is_the_one_core_calls() -> None:
    openapi = (PACKAGE.parents[2] / "services" / "core" / "openapi.yaml").read_text()
    assert "x-hr-target-path: /api/method/kamra_uy.fiscal.webhook.core_event" in openapi
    assert _defined("kamra_uy.fiscal.webhook.core_event")
