"""Мінімальний клієнт Admin API WireMock (лише стандартна бібліотека Python).

Використовується для:
  * налаштування stub-відповідей (mappings) під конкретний сценарій;
  * очищення mappings і request journal між тестами;
  * перевірки фактичних HTTP-запитів, які надіслав SUT.
"""
import http.client
import json
import time
from urllib.parse import urlsplit

POLICY_URL = "/rental-policy"


class WireMockClient:
    def __init__(self, base_url: str):
        address = urlsplit(base_url)
        self._host, self._port = address.hostname, address.port

    # ---------- низькорівневий виклик Admin API ----------
    def _admin(self, method: str, path: str, body: dict | None = None):
        connection = http.client.HTTPConnection(self._host, self._port, timeout=10)
        try:
            payload = json.dumps(body).encode("utf-8") if body is not None else None
            headers = {"Content-Type": "application/json"} if payload is not None else {}
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            if response.status >= 400:
                raise RuntimeError(f"WireMock admin {method} {path}: {response.status} {raw!r}")
            return json.loads(raw) if raw else None
        finally:
            connection.close()

    # ---------- життєвий цикл ----------
    def wait_until_ready(self, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._admin("GET", "/__admin/mappings")
                return
            except (OSError, http.client.HTTPException, RuntimeError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.3)

    def reset(self) -> None:
        """Видаляє всі mappings і очищає request journal."""
        self._admin("POST", "/__admin/reset")

    # ---------- налаштування відповідей ----------
    def stub_policy(self, *, status: int = 200, json_body: dict | None = None,
                    raw_body: str | None = None, delay_ms: int = 0) -> None:
        response: dict = {"status": status, "headers": {"Content-Type": "application/json"}}
        if json_body is not None:
            response["jsonBody"] = json_body
        if raw_body is not None:
            response["body"] = raw_body
        if delay_ms:
            response["fixedDelayMilliseconds"] = delay_ms
        self._admin("POST", "/__admin/mappings", {
            "request": {"method": "POST", "url": POLICY_URL},
            "response": response,
        })

    def stub_allowed(self, allowed: bool) -> None:
        self.stub_policy(status=200, json_body={"allowed": allowed})

    # ---------- перевірка журналу запитів ----------
    def policy_requests(self) -> list[dict]:
        """Запити POST /rental-policy, які фактично отримав WireMock."""
        found = self._admin("POST", "/__admin/requests/find", {"method": "POST", "url": POLICY_URL})
        return [entry for entry in found["requests"]]

    def total_requests(self) -> int:
        """Кількість УСІХ запитів у журналі (на будь-який endpoint)."""
        return self._admin("GET", "/__admin/requests")["meta"]["total"]

    @staticmethod
    def body_of(request: dict) -> dict:
        return json.loads(request["body"])

    @staticmethod
    def header_of(request: dict, name: str) -> str | None:
        for key, value in request.get("headers", {}).items():
            if key.lower() == name.lower():
                return value
        return None
