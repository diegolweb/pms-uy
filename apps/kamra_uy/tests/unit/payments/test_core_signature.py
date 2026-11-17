"""La firma X-HR-Signature de los avisos de core (HR-30), sin frappe, contra vectores generados aparte."""

import json
from pathlib import Path

import pytest

from kamra_uy.pure.core_signature import TOLERANCE_SECONDS, sign, verify

VECTORS = json.loads((Path(__file__).parent / "core-signature-vectors.json").read_text())


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["name"])
def test_the_vectors(case) -> None:
    got = verify(VECTORS["secret"], case["header"], case["body"].encode(), VECTORS["now"])
    assert got == tuple(case["expect"])


def test_sign_matches_the_good_vector() -> None:
    good = VECTORS["cases"][0]
    assert good["header"] == f"t={VECTORS['now']},v1={sign(VECTORS['secret'], VECTORS['now'], good['body'].encode())}"


def test_the_tolerance_is_five_minutes() -> None:
    assert TOLERANCE_SECONDS == 300


@pytest.mark.parametrize("header", [None, "", "t=,v1=", "t=1795000000"])
def test_a_missing_or_partial_header_is_malformed(header) -> None:
    assert verify("s", header, b"{}", 1795000000) == (False, "malformed")


def test_a_custom_tolerance_is_honoured() -> None:
    body = b"{}"
    header = f"t=1000,v1={sign('s', 1000, body)}"
    assert verify("s", header, body, 1010, tolerance=5) == (False, "stale")
    assert verify("s", header, body, 1010, tolerance=10) == (True, "ok")
