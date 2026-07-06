"""`loopy deploy render` — provision the engine on Render.com from the operator's API key.

The Render deploy target (see `loopy_cli.deploy_target`): Render builds the project's
generated Dockerfile from its git repo (git-push CD), so this command's job is the
gap the dashboard would otherwise fill — verify the repo/Dockerfile/secrets are
deployable (preflight), create-or-update the web service via `api.render.com/v1`,
push env vars + secret files, poll the deploy to live, write LOOPY_PUBLIC_URL back
to loopy.env, and register GitHub webhooks. Design: docs/design/render-deploy.md.

httpx is a core dependency (the admin proxy uses it); imported lazily in the client
so `loopy compile` never loads it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import typer

from loopy_cli.deploy_cmd import deploy_app

RENDER_API_URL = "https://api.render.com/v1"
RENDER_API_KEY_ENV = "RENDER_API_KEY"
RENDER_KEYS_URL = "https://dashboard.render.com/settings#api-keys"

# Render Secret Files mount flat at /etc/secrets/<name> (Docker services see them ONLY
# there), so nested env_file paths encode `/` as `__`; the generated Dockerfile's start
# command decodes (see _DOCKERFILE_TEMPLATE in loopy_cli). Kept in sync by the tests.
SECRET_NAME_SEP = "__"


def encode_secret_file_name(rel_path: str) -> str:
    """Flatten a project-relative env-file path into a Render secret-file name.

    Lossy if a segment itself contains `__` — rejected here so the deploy fails loudly
    at upload time instead of the boot shim linking the wrong path.
    """
    if SECRET_NAME_SEP in rel_path:
        raise ValueError(
            f"secret file path {rel_path!r} contains {SECRET_NAME_SEP!r}, which the "
            "flat-name encoding reserves for '/'; rename the file"
        )
    return rel_path.replace("/", SECRET_NAME_SEP)


class RenderAPIError(RuntimeError):
    """A Render API failure with the platform's own message and the one next action."""

    def __init__(self, status: int, message: str, hint: str = ""):
        self.status = status
        self.message = message
        self.hint = hint
        text = f"Render API error {status}: {message}"
        if hint:
            text += f"\n  → {hint}"
        super().__init__(text)


def _hint_for(status: int, message: str) -> str:
    """The one next action for an API failure — every error names its fix."""
    lowered = message.lower()
    if status == 401:
        return f"key invalid or revoked — mint a new one at {RENDER_KEYS_URL}"
    if status in (402, 403) and ("plan" in lowered or "payment" in lowered):
        return "the chosen plan needs a payment method on the workspace (dashboard → Billing)"
    if status == 429:
        return "rate limited — wait a minute and re-run (the command is idempotent)"
    if "repo" in lowered and ("not found" in lowered or "connect" in lowered or "permission" in lowered):
        return (
            "private repo? connect GitHub to Render once (dashboard.render.com → New → "
            "Web Service → connect your account), then re-run"
        )
    return ""


class RenderClient:
    """Thin `api.render.com/v1` wrapper: bearer auth, JSON, readable errors.

    `transport` is injectable for tests (httpx.MockTransport); production uses the default.
    """

    def __init__(self, api_key: str, *, transport=None):
        import httpx

        self._http = httpx.Client(
            base_url=RENDER_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=30.0,
            transport=transport,
        )

    def _request(self, method: str, path: str, **kwargs):
        import httpx

        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise RenderAPIError(
                0,
                f"{type(exc).__name__}: {exc}",
                "network problem reaching api.render.com — check connectivity and re-run",
            ) from exc
        if response.status_code >= 400:
            try:
                message = response.json().get("message") or response.text
            except ValueError:
                message = response.text
            raise RenderAPIError(response.status_code, message, _hint_for(response.status_code, message))
        return response

    def owners(self) -> list[dict]:
        return [item["owner"] for item in self._request("GET", "/owners").json()]

    def find_service(self, name: str) -> dict | None:
        items = self._request("GET", "/services", params={"name": name, "limit": 20}).json()
        for item in items:
            service = item.get("service", item)
            if service.get("name") == name:
                return service
        return None

    def get_service(self, service_id: str) -> dict | None:
        try:
            return self._request("GET", f"/services/{service_id}").json()
        except RenderAPIError as exc:
            if exc.status == 404:
                return None
            raise

    def create_service(self, payload: dict) -> dict:
        body = self._request("POST", "/services", json=payload).json()
        return body.get("service", body)  # create returns {service, deployId}

    def update_service(self, service_id: str, payload: dict) -> dict:
        return self._request("PATCH", f"/services/{service_id}", json=payload).json()

    def put_env_vars(self, service_id: str, env: dict[str, str]) -> None:
        body = [{"key": key, "value": env[key]} for key in sorted(env)]
        self._request("PUT", f"/services/{service_id}/env-vars", json=body)

    def put_secret_files(self, service_id: str, files: dict[str, str]) -> None:
        body = [{"name": name, "content": files[name]} for name in sorted(files)]
        self._request("PUT", f"/services/{service_id}/secret-files", json=body)

    def trigger_deploy(self, service_id: str) -> dict:
        return self._request("POST", f"/services/{service_id}/deploys", json={}).json()

    def get_deploy(self, service_id: str, deploy_id: str) -> dict:
        return self._request("GET", f"/services/{service_id}/deploys/{deploy_id}").json()

    def delete_service(self, service_id: str) -> None:
        self._request("DELETE", f"/services/{service_id}")
