"""El cliente HTTP del servicio de cobros que Kamra usa para Uruguay (HR-30; también lo usará la facturación).

Habla JSON con `Authorization: Bearer <clave>`. Un fallo de red o un 5xx se reintenta una vez, con la MISMA
`Idempotency-Key` en un POST, así que el servicio nunca hace dos veces lo mismo. Los errores `application/problem+json`
llegan como `CoreError`. La clave no sale nunca en un log, un repr ni una excepción.

Sin frappe arriba: `core_base_url` lo importa dentro. La sesión es inyectable (por defecto, `requests.Session`).
"""

from __future__ import annotations

import json

from kamra_uy.payments import texts

DEFAULT_TIMEOUT = 15
RETRYABLE_CODES = ("upstream_unavailable", "idempotency_in_progress", "not_ready")


class CoreError(Exception):
    """Un error del servicio de cobros: `status` (None si no respondió), `code`, `detail`, `retryable`, `errors`."""

    def __init__(self, status: int | None, code: str, detail: str = "", retryable: bool = False,
                 errors: list | None = None):
        self.status = status
        self.code = code
        self.detail = detail
        self.retryable = retryable
        self.errors = list(errors or [])
        where = "; ".join(f"{e.get('path')}: {e.get('message')}" for e in self.errors if isinstance(e, dict))
        super().__init__(" — ".join(x for x in (f"{code} ({status or 'sin respuesta'})", detail, where) if x))


class CoreClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = DEFAULT_TIMEOUT, session=None):
        if not base_url:
            raise CoreError(None, "not_configured", texts.NOT_CONFIGURED)
        if not api_key:
            raise CoreError(None, "not_configured", "falta la clave del servicio de cobros")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._key = api_key
        if session is None:
            import requests

            session = requests.Session()
        self._session = session

    def __repr__(self) -> str:
        return f"CoreClient({self.base_url!r})"

    __str__ = __repr__

    def post(self, path: str, body: dict, idempotency_key: str) -> dict:
        return self._call("POST", path, body, idempotency_key)

    def get(self, path: str) -> dict:
        return self._call("GET", path, None, None)

    def _call(self, method: str, path: str, body: dict | None, idempotency_key: str | None) -> dict:
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        url = f"{self.base_url}/{path.lstrip('/')}"
        for attempt in (1, 2):
            try:
                resp = self._session.request(method, url, data=data, headers=headers, timeout=self.timeout)
            except OSError:  # requests.RequestException (red, tiempo) es un OSError
                if attempt == 2:
                    raise CoreError(None, "upstream_unavailable", f"{method} {path}: sin respuesta",
                                    retryable=True) from None
                continue
            if resp.status_code >= 500 and attempt == 1:
                continue
            return _result(resp)
        raise AssertionError("unreachable")  # pragma: no cover


def _result(resp) -> dict:
    status = resp.status_code
    try:
        parsed = json.loads(resp.content) if resp.content else {}
    except (UnicodeDecodeError, ValueError):
        parsed = None
    if status < 400:
        if not isinstance(parsed, dict):
            raise CoreError(status, "bad_response", "la respuesta no es un objeto JSON")
        return parsed
    problem = parsed if isinstance(parsed, dict) else {}
    code = problem.get("code") if isinstance(problem.get("code"), str) else f"http_{status}"
    detail = problem.get("detail") or problem.get("title") or ""
    retryable = status >= 500 or code in RETRYABLE_CODES
    raise CoreError(status, code, str(detail), retryable=retryable, errors=problem.get("errors"))


def core_base_url() -> str:
    """`hr_core_base_url` de site_config.json. Sin ella, CoreError con el texto para recepción."""
    import frappe

    url = (frappe.conf.get("hr_core_base_url") or "").strip()
    if not url:
        raise CoreError(None, "not_configured", texts.NOT_CONFIGURED)
    return url
