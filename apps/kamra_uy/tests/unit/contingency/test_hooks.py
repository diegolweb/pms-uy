"""Los ganchos del modo contingencia en hooks.py (HR-34), leídos sin ejecutar el módulo."""

import ast
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[3] / "kamra_uy" / "hooks.py"
HANDLER = "kamra_uy.contingency.alerts.on_reservation_change"


def _hooks() -> dict:
    values = {}
    for node in ast.parse(HOOKS.read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            values[node.targets[0].id] = ast.literal_eval(node.value)
    return values


def test_the_daily_report_runs_every_hour_and_decides_by_the_hotel_hour() -> None:
    assert _hooks()["scheduler_events"]["cron"]["0 * * * *"] == ["kamra_uy.contingency.alerts.daily_report"]


def test_every_reservation_change_is_hooked() -> None:
    # otros ganchos pueden ir en el mismo evento (el conector fiscal de HR-28 en on_update): basta con que esté este
    reservation = _hooks()["doc_events"]["Reservation"]
    for event in ("after_insert", "on_update", "on_cancel", "on_trash"):
        handlers = reservation[event] if isinstance(reservation[event], list) else [reservation[event]]
        assert HANDLER in handlers, event


def test_the_uruguay_hooks_are_still_there() -> None:
    hooks = _hooks()
    assert hooks["kamra_localization"] == {"Uruguay": "kamra_uy.localization.uruguay"}
    assert len(hooks["fixtures"]) == 2
