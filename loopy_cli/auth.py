"""`loopy auth github` — one-command GitHub App onboarding via the manifest flow.

Walks a self-hosting user through creating *their own* GitHub App and lands its
credentials locally, so loopy can later mint short-lived, repo-scoped tokens for
agents — no loopy-owned central app, no persistent server.

The flow (see plans/future/repo-access/2026-06-18-loopy-auth-github.md):
  1. build a manifest (minimal perms, no webhook) pointing at a local callback;
  2. serve a one-shot 127.0.0.1 listener (the `gh auth login` pattern);
  3. open the browser to a local page that auto-POSTs the manifest to GitHub;
  4. GitHub redirects back with a temporary `?code=`; exchange it for creds;
  5. persist the PEM (gitignored, 0600) + ids into `loopy.env`;
  6. print the install URL so the user picks which repos the App can touch;
  7. best-effort verify by minting a token.

Credential mechanics live in `loopy_runtime.scm.github_app`; this module owns the
browser/callback/manifest orchestration. Heavy imports (runtime, http, jwt) are
deferred into command bodies so `loopy compile` stays runtime-free.
"""

from __future__ import annotations

import html
import json
import os
import secrets
from pathlib import Path

import typer

auth_app = typer.Typer(no_args_is_help=True, help="Authenticate loopy with external services.")

# Suggested App homepage (required by the manifest schema).
HOMEPAGE_URL = "https://github.com/peterzakin/loopy"
DEFAULT_PORT = 8765
CALLBACK_TIMEOUT_SECONDS = 300


def build_manifest(name: str, redirect_url: str, *, public: bool = False) -> dict:
    """Assemble the GitHub App manifest: minimal fix/PR permissions, no webhook.

    `hook_attributes` is deliberately omitted: GitHub requires `hook_attributes.url`
    whenever the object is present (sending `{active: false}` alone fails with
    "url wasn't supplied"). An App with no `hook_attributes` simply has no webhook —
    which is what we want, since this App is a credential source, not an event sink,
    so loopy stays serverless.
    """
    return {
        "name": name,
        "url": HOMEPAGE_URL,
        "redirect_url": redirect_url,
        "public": public,
        "default_permissions": {
            "contents": "write",
            "pull_requests": "write",
            "metadata": "read",
        },
    }


def create_app_url(org: str | None) -> str:
    """GitHub's create-app endpoint — org-scoped if `org` is given, else personal."""
    if org:
        return f"https://github.com/organizations/{org}/settings/apps/new"
    return "https://github.com/settings/apps/new"


def default_app_name(org: str | None) -> str:
    """A unique-by-construction default name (GitHub App names are global).

    GitHub App names must be globally unique and can't shadow an existing account —
    bare "loopy" is reserved for @loopy — so the auto-default appends a short random
    suffix to make a collision astronomically unlikely. Pass `--name` to choose your
    own; whatever name the App ends up with (incl. edits on GitHub's create screen) is
    read back from the conversion response's `slug`, so the rest of the flow follows.
    """
    base = f"loopy-{org}" if org else "loopy"
    return f"{base}-{secrets.token_hex(2)}"


def render_submit_page(action_url: str, manifest: dict, state: str) -> str:
    """A tiny local page that auto-POSTs the manifest to GitHub's create-app URL.

    Follows GitHub's documented manifest-flow pattern: the `manifest` field value is
    set in JavaScript via `JSON.stringify`, not baked into the HTML attribute. Setting
    it in the DOM avoids attribute-escaping pitfalls — embedding escaped JSON in the
    `value` attribute is what made GitHub reject the manifest ("url wasn't supplied").
    """
    action = f"{html.escape(action_url, quote=True)}?state={html.escape(state, quote=True)}"
    # A JS string literal of the manifest JSON; <-escape so a "</script>" in any value
    # (e.g. an app name) can't break out of the script element.
    manifest_literal = json.dumps(json.dumps(manifest)).replace("<", "\\u003c")
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>loopy → GitHub</title>"
        "</head><body style='font-family:system-ui;text-align:center;padding:48px'>"
        "<p>Redirecting you to GitHub to create your loopy App…</p>"
        f"<form id='f' method='post' action='{action}'>"
        "<input type='hidden' name='manifest' id='manifest'>"
        "<noscript><button type='submit'>Continue to GitHub</button></noscript>"
        f"</form><script>document.getElementById('manifest').value = {manifest_literal};"
        "document.getElementById('f').submit();</script>"
        "</body></html>"
    )


_SUCCESS_PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'><title>loopy</title></head>"
    "<body style='font-family:system-ui;text-align:center;padding:48px'>"
    "<h2>✓ App created.</h2><p>You can close this tab and return to your terminal.</p>"
    "</body></html>"
).encode()

_ERROR_PAGE = (
    b"<!doctype html><html><head><meta charset='utf-8'><title>loopy</title></head>"
    b"<body style='font-family:system-ui;text-align:center;padding:48px'>"
    b"<h2>Something went wrong.</h2><p>Return to your terminal and try again.</p>"
    b"</body></html>"
)


def obtain_manifest_code(
    *, name: str, org: str | None, port: int, open_browser: bool = True
) -> str:
    """Run the browser + one-shot callback dance and return the temporary code.

    Binds a 127.0.0.1 listener (ephemeral if `port` is 0), serves a page that
    auto-submits the manifest to GitHub, and waits for GitHub's redirect back
    with `?code=&state=`. The listener is torn down before returning.
    """
    import http.server
    import threading
    import urllib.parse
    import webbrowser

    state_token = secrets.token_urlsafe(16)
    captured: dict[str, str] = {}
    done = threading.Event()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):  # silence default stderr logging
            return

        def _send(self, code: int, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            ctx = self.server.loopy_ctx  # type: ignore[attr-defined]
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self._send(200, ctx["submit_page"].encode())
                return
            if parsed.path == "/callback":
                params = urllib.parse.parse_qs(parsed.query)
                code = (params.get("code") or [""])[0]
                state = (params.get("state") or [""])[0]
                if code and state == ctx["state"]:
                    captured["code"] = code
                    self._send(200, _SUCCESS_PAGE)
                else:
                    self._send(400, _ERROR_PAGE)
                done.set()
                return
            self._send(404, b"not found")

    server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    actual_port = server.server_address[1]
    redirect_url = f"http://127.0.0.1:{actual_port}/callback"
    manifest = build_manifest(name, redirect_url)
    server.loopy_ctx = {  # type: ignore[attr-defined]
        "submit_page": render_submit_page(create_app_url(org), manifest, state_token),
        "state": state_token,
    }

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        start_url = f"http://127.0.0.1:{actual_port}/"
        typer.echo(f"  → opening browser to create the app: {start_url}")
        if open_browser:
            webbrowser.open(start_url)
        else:
            typer.echo("  → --no-browser: open the URL above manually")
        if not done.wait(CALLBACK_TIMEOUT_SECONDS):
            raise typer.Exit(code=1)
    finally:
        server.shutdown()
        thread.join(timeout=5)

    code = captured.get("code")
    if not code:
        typer.echo("error: did not receive a valid code from GitHub", err=True)
        raise typer.Exit(code=1)
    return code


def _escape_pem(pem: str) -> str:
    """Collapse a multi-line PEM to one dotenv-safe line (newlines → literal `\\n`)."""
    return pem.replace("\r\n", "\n").replace("\n", "\\n")


def write_app_credentials(root: str | Path, conversion: dict) -> Path:
    """Persist a manifest conversion locally: App id + private key, inline in loopy.env.

    The key is stored inline (`GITHUB_APP_PRIVATE_KEY`, newlines escaped) rather than as a
    file referenced by a relative path. A path resolves against whatever `--root` a later
    command uses, so `trigger --root <subdir>` silently failed to find a key written
    relative to the project root — an inline key has no such dependency. Because loopy.env
    now carries the private key, it's added to .gitignore. Returns the loopy.env path.
    """
    from loopy_runtime.secrets import CONTROL_PLANE_ENV_FILE, write_control_plane_env

    root = Path(root)
    app_id = str(conversion["id"])
    pem = conversion["pem"]

    env_path = write_control_plane_env(
        root,
        {"GITHUB_APP_ID": app_id, "GITHUB_APP_PRIVATE_KEY": _escape_pem(pem)},
    )
    _ensure_gitignored(root, CONTROL_PLANE_ENV_FILE)
    return env_path


def _ensure_gitignored(root: Path, entry: str) -> None:
    """Append `entry` to .gitignore if not already present (idempotent)."""
    path = root / ".gitignore"
    lines = path.read_text().splitlines() if path.is_file() else []
    if any(line.strip() == entry for line in lines):
        return
    with path.open("a") as fh:
        if lines and lines[-1].strip():
            fh.write("\n")
        fh.write(f"# loopy local state (GitHub App private key, etc.)\n{entry}\n")


def _load_creds(root: str | Path):  # -> AppCredentials
    """Resolve App credentials from loopy.env merged under the process env (env wins)."""
    from loopy_runtime.scm import github_app
    from loopy_runtime.secrets import load_control_plane_env

    merged = dict(os.environ)
    for key, value in load_control_plane_env(root).items():
        merged.setdefault(key, value)
    return github_app.AppCredentials.from_env(merged, root=root)


def _verify(root: str | Path) -> None:
    """Best-effort: mint a token and report the installations/repos it can see."""
    from loopy_runtime.scm import github_app

    try:
        creds = _load_creds(root)
        installations = github_app.list_installations(creds)
    except github_app.GitHubAppError as exc:
        typer.echo(f"  (skipped verify: {exc})")
        return

    if not installations:
        typer.echo("  ⓘ App created but not installed yet — install it, then re-run verify.")
        return

    for installation in installations:
        install_id = installation.get("id")
        account = (installation.get("account") or {}).get("login", "?")
        try:
            token = github_app.mint_installation_token(creds, install_id)["token"]
            repos = github_app.list_installation_repositories(token)
            count = repos.get("total_count", len(repos.get("repositories", [])))
        except github_app.GitHubAppError as exc:
            typer.echo(f"  ✗ installation {install_id} ({account}): {exc}")
            continue
        typer.echo(
            typer.style("  ✓", fg=typer.colors.GREEN)
            + f" installation {install_id} ({account}) — token minted, {count} repo(s) reachable"
        )


@auth_app.command()
def github(
    org: str | None = typer.Option(None, "--org", help="Create under this org (default: account)."),
    name: str | None = typer.Option(None, "--name", help="App name (default: loopy[-org])."),
    port: int = typer.Option(DEFAULT_PORT, "--port", help="Local callback port (0 = ephemeral)."),
    root: Path = typer.Option(Path("."), "--root", help="Project root (where loopy.env lives)."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing stored App credentials."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the URL, don't open it."),
) -> None:
    """Create your own GitHub App via the manifest flow and store its credentials."""
    from loopy_runtime.scm import github_app
    from loopy_runtime.secrets import load_control_plane_env

    if not force and load_control_plane_env(root).get("GITHUB_APP_ID"):
        typer.echo(
            "error: GITHUB_APP_ID already set in loopy.env — re-run with --force to overwrite.",
            err=True,
        )
        raise typer.Exit(code=1)

    app_name = name or default_app_name(org)
    typer.echo(typer.style("\n  🔐  loopy auth github", fg=typer.colors.CYAN, bold=True))
    typer.echo(typer.style("  " + "─" * 40, fg=typer.colors.BRIGHT_BLACK))
    where = f"org '{org}'" if org else "your personal account"
    typer.echo(f"  → creating GitHub App '{app_name}' under {where}")

    code = obtain_manifest_code(name=app_name, org=org, port=port, open_browser=not no_browser)
    try:
        conversion = github_app.exchange_manifest_code(code)
    except github_app.GitHubAPIError as exc:
        typer.echo(f"error: failed to exchange manifest code: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    env_path = write_app_credentials(root, conversion)
    typer.echo(
        typer.style("  ✓", fg=typer.colors.GREEN)
        + f" wrote App id + private key to {env_path} (gitignored)"
    )

    slug = conversion.get("slug")
    if slug:
        install_url = f"https://github.com/apps/{slug}/installations/new"
        typer.echo("\n  Next: install the App on the repos loopy should access:")
        typer.echo(typer.style(f"    {install_url}", fg=typer.colors.BLUE))

    typer.echo("\n  Verifying credentials…")
    _verify(root)
    typer.echo()


@auth_app.command()
def status(
    root: Path = typer.Option(Path("."), "--root", help="Project root (where loopy.env lives)."),
) -> None:
    """Show stored GitHub App credentials and verify they can mint tokens."""
    from loopy_runtime.scm import github_app

    try:
        creds = _load_creds(root)
    except github_app.MissingCredentials as exc:
        typer.echo(f"no GitHub App configured: {exc}", err=True)
        typer.echo("run `loopy auth github` to set one up.", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(typer.style("  ✓", fg=typer.colors.GREEN) + f" GitHub App id {creds.app_id} set")
    _verify(root)
