"""Por qué no vale un código de descuento (HR-377, kamra_uy/pure/voucher.py): el motivo que la web traduce."""

from datetime import date

import pytest

from kamra_uy.pure.voucher import motivo

HOY = date(2026, 11, 27)
VALE = {"disabled": 0, "valid_from": None, "valid_to": None, "max_uses": 0, "times_used": 0, "min_nights": 1}


@pytest.mark.parametrize(("cambio", "noches", "esperado"), [
    ({}, 3, None),
    (None, 3, {"reason": "no-existe"}),
    ({"disabled": 1, "valid_to": "2026-01-01"}, 3, {"reason": "inactivo"}),  # el orden de Kamra: inactivo antes
    ({"valid_from": "2026-12-01"}, 3, {"reason": "aun-no", "date": "2026-12-01"}),
    ({"valid_to": date(2026, 11, 26)}, 3, {"reason": "vencido", "date": "2026-11-26"}),
    ({"max_uses": 5, "times_used": 5}, 3, {"reason": "agotado"}),
    ({"min_nights": 4}, 3, {"reason": "noches", "min_nights": 4}),
])
def test_motivo(cambio, noches, esperado) -> None:
    v = None if cambio is None else {**VALE, **cambio}
    assert motivo(v, HOY, noches) == esperado
