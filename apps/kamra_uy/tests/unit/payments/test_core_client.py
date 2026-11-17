"""El cliente HTTP del servicio de cobros (HR-30), sin frappe ni red: la sesión es un doble."""

import json

import pytest

from kamra_uy.core.client import CoreClient, CoreError

KEY = "test-core-key-not-a-real-one-0000"


class Response:
    def __init__(self, status: int, body=None, raw: bytes | None = None):
        self.status_code = status
        self.content = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())


class Session:
    """Devuelve (o lanza) lo que se le da, en orden, y apunta cada petición."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def request(self, method, url, *, data=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "data": data, "headers": headers, "timeout": timeout})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def problem(status, code, detail="", **extra):
    return {"type": f"urn:hr-core:problem:{code}", "title": code, "status": status, "code": code,
            "detail": detail, "request_id": "req-1", **extra}


def client(*outcomes, base="http://core:8080/"):
    session = Session(*outcomes)
    return CoreClient(base, KEY, timeout=7, session=session), session


def test_post_sends_the_key_the_idempotency_key_and_the_body() -> None:
    core, session = client(Response(201, {"link_id": "l1", "url": "http://mp/1"}))
    assert core.post("/v1/payments/links", {"amount": "1.00"}, "idem-1") == {"link_id": "l1", "url": "http://mp/1"}
    (call,) = session.calls
    assert call["method"] == "POST"
    assert call["url"] == "http://core:8080/v1/payments/links"
    assert call["headers"]["Authorization"] == f"Bearer {KEY}"
    assert call["headers"]["Idempotency-Key"] == "idem-1"
    assert call["headers"]["Content-Type"] == "application/json"
    assert json.loads(call["data"]) == {"amount": "1.00"}
    assert call["timeout"] == 7


def test_get_reads_without_an_idempotency_key() -> None:
    core, session = client(Response(200, {"status": "paid"}))
    assert core.get("/v1/payments/links/l1") == {"status": "paid"}
    (call,) = session.calls
    assert call["method"] == "GET"
    assert "Idempotency-Key" not in call["headers"]
    assert call["data"] is None


def test_an_empty_success_is_an_empty_dict() -> None:
    core, _ = client(Response(202))
    assert core.post("/x", {}, "k") == {}


@pytest.mark.parametrize("first", [ConnectionError("down"), TimeoutError("slow"), Response(502, raw=b"bad gateway")])
def test_one_retry_on_the_network_or_a_5xx_with_the_same_key(first) -> None:
    core, session = client(first, Response(201, {"ok": True}))
    assert core.post("/x", {}, "idem-2") == {"ok": True}
    assert [c["headers"]["Idempotency-Key"] for c in session.calls] == ["idem-2", "idem-2"]


def test_get_retries_once_too() -> None:
    core, session = client(Response(503, problem(503, "not_ready")), Response(200, {"a": 1}))
    assert core.get("/x") == {"a": 1}
    assert len(session.calls) == 2


def test_two_5xx_give_the_problem_of_the_last() -> None:
    core, session = client(Response(503, problem(503, "upstream_unavailable", "MP caído")),
                           Response(503, problem(503, "upstream_unavailable", "MP caído")))
    with pytest.raises(CoreError) as err:
        core.post("/x", {}, "k")
    assert (err.value.status, err.value.code, err.value.detail, err.value.retryable) == (
        503, "upstream_unavailable", "MP caído", True)
    assert len(session.calls) == 2


def test_the_network_twice_is_upstream_unavailable_and_leaks_nothing() -> None:
    core, session = client(ConnectionError(f"Authorization: Bearer {KEY}"), ConnectionError("down"))
    with pytest.raises(CoreError) as err:
        core.post("/x", {}, "k")
    assert (err.value.status, err.value.code, err.value.retryable) == (None, "upstream_unavailable", True)
    assert err.value.__cause__ is None and err.value.__suppress_context__
    assert KEY not in str(err.value) and KEY not in repr(err.value)
    assert len(session.calls) == 2


def test_a_4xx_problem_is_not_retried() -> None:
    errors = [{"path": "/body/payer", "message": "falta name"}]
    core, session = client(Response(422, problem(422, "validation_failed", "cuerpo no válido", errors=errors)))
    with pytest.raises(CoreError) as err:
        core.post("/x", {}, "k")
    assert (err.value.status, err.value.code, err.value.retryable) == (422, "validation_failed", False)
    assert err.value.errors == errors
    assert "/body/payer" in str(err.value)
    assert len(session.calls) == 1


def test_an_error_without_a_problem_body_still_has_a_code() -> None:
    core, _ = client(Response(404, raw=b"<html>nope</html>"))
    with pytest.raises(CoreError) as err:
        core.get("/x")
    assert (err.value.status, err.value.code, err.value.retryable) == (404, "http_404", False)


def test_idempotency_in_progress_is_retryable() -> None:
    core, _ = client(Response(409, problem(409, "idempotency_in_progress")))
    with pytest.raises(CoreError) as err:
        core.post("/x", {}, "k")
    assert err.value.retryable


def test_a_success_that_is_not_json_is_an_error() -> None:
    core, _ = client(Response(200, raw=b"not json"))
    with pytest.raises(CoreError) as err:
        core.get("/x")
    assert err.value.code == "bad_response"


def test_the_key_never_shows_in_the_client() -> None:
    core, _ = client()
    assert KEY not in repr(core) and KEY not in str(core)


def test_a_client_needs_a_base_url_and_a_key() -> None:
    with pytest.raises(CoreError):
        CoreClient("", KEY, session=Session())
    with pytest.raises(CoreError):
        CoreClient("http://core:8080", "", session=Session())
