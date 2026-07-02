"""`loopy auth github` — one-command GitHub App onboarding via the manifest flow.

Walks a self-hosting user through creating *their own* GitHub App and lands its
credentials locally, so loopy can later mint short-lived, repo-scoped tokens for
agents — no loopy-owned central app, no persistent server.

The flow:
  1. build a manifest (minimal perms) pointing at a local callback — with a webhook baked
     in when the project has a public URL (`LOOPY_PUBLIC_URL` / registry.yml `public_url`),
     without one otherwise;
  2. serve a one-shot 127.0.0.1 listener (the `gh auth login` pattern);
  3. open the browser to a local page that auto-POSTs the manifest to GitHub;
  4. GitHub redirects back with a temporary `?code=`; exchange it for creds;
  5. persist the App id + private key (and the webhook secret, when GitHub minted one)
     inline, gitignored, into `loopy.env`;
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
from collections.abc import Mapping
from pathlib import Path

import typer

auth_app = typer.Typer(no_args_is_help=True, help="Authenticate loopy with external services.")

# Suggested App homepage (required by the manifest schema).
HOMEPAGE_URL = "https://github.com/peterzakin/loopy"
DEFAULT_PORT = 8765
CALLBACK_TIMEOUT_SECONDS = 300

# The canonical GitHub ingress path `loopy run` serves — the webhook we register points here.
GITHUB_WEBHOOK_PATH = "/hooks/github"


def build_manifest(
    name: str, redirect_url: str, *, public: bool = False, webhook_url: str | None = None
) -> dict:
    """Assemble the GitHub App manifest: minimal fix/PR permissions, webhook optional.

    With `webhook_url` set, `hook_attributes` registers the App's webhook at creation
    time — the only moment GitHub mints a webhook secret for us (returned in the
    conversion response to persist). Without it, `hook_attributes` is deliberately
    omitted: GitHub requires `hook_attributes.url` whenever the object is present
    (sending `{active: false}` alone fails with "url wasn't supplied"). An App with no
    `hook_attributes` simply has no webhook — the App stays a pure credential source
    and loopy stays serverless.
    """
    manifest = {
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
    if webhook_url:
        manifest["hook_attributes"] = {"url": webhook_url, "active": True}
        # A webhook needs event subscriptions or GitHub delivers nothing. Cover the
        # built-in `Github.*` catalog (PRs, issues, issue comments, pushes).
        manifest["default_events"] = ["pull_request", "issues", "issue_comment", "push"]
        # `issues`/`issue_comment` events require issue read access; contents:write
        # (already granted) covers `push`.
        manifest["default_permissions"]["issues"] = "read"
    return manifest


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
    *,
    name: str,
    org: str | None,
    port: int,
    open_browser: bool = True,
    webhook_url: str | None = None,
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
    manifest = build_manifest(name, redirect_url, webhook_url=webhook_url)
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

    When the App was created with a webhook (`hook_attributes` in the manifest), GitHub
    mints a webhook secret and returns it in the conversion — persist it as
    `GITHUB_WEBHOOK_SECRET` so `loopy run` verifies deliveries without any manual step.
    """
    from loopy_runtime.secrets import CONTROL_PLANE_ENV_FILE, write_control_plane_env

    root = Path(root)
    app_id = str(conversion["id"])
    pem = conversion["pem"]

    updates = {"GITHUB_APP_ID": app_id, "GITHUB_APP_PRIVATE_KEY": _escape_pem(pem)}
    if conversion.get("webhook_secret"):
        updates["GITHUB_WEBHOOK_SECRET"] = conversion["webhook_secret"]
    env_path = write_control_plane_env(root, updates)
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


# After the manifest flow *creates* the App, installing it on repos is a separate manual
# step on GitHub. Poll this often, for at most this long, for the install to land before
# giving up and pointing the user at `loopy auth status` to finish later.
_INSTALL_POLL_INTERVAL_SECONDS = 3
_INSTALL_WAIT_TIMEOUT_SECONDS = 300


def _wait_for_installation(
    root: str | Path,
    *,
    timeout: float = _INSTALL_WAIT_TIMEOUT_SECONDS,
    poll_interval: float = _INSTALL_POLL_INTERVAL_SECONDS,
    sleep=None,
    monotonic=None,
) -> list[dict] | None:
    """Block until the freshly-created App reports at least one installation.

    The manifest flow only *creates* the App; installing it on the repos it should touch
    is a separate manual action on GitHub. Printing the install URL and exiting left a
    created-but-uninstalled App that only failed much later, at `loopy run`, with "the
    GitHub App has no installations" — far from where the install was skipped. Instead,
    poll here until the install lands, so the common "I forgot to click Install" case is
    caught at auth time, right where the URL was printed.

    Returns the installations once non-empty, or None if the user skips (Ctrl-C), the wait
    times out, or the creds can't be read. Timing is injected in tests so they don't sleep.
    """
    import time

    from loopy_runtime.scm import github_app

    sleep = sleep or time.sleep
    monotonic = monotonic or time.monotonic

    try:
        creds = _load_creds(root)
    except github_app.GitHubAppError as exc:
        typer.echo(f"  (skipped install wait: {exc})")
        return None

    typer.echo("\n  Waiting for the install to land… (Ctrl-C to skip and finish later)")
    deadline = monotonic() + timeout
    try:
        while True:
            try:
                installations = github_app.list_installations(creds)
            except github_app.GitHubAppError as exc:
                typer.echo(f"  (stopped waiting: {exc})")
                return None
            if installations:
                return installations
            if monotonic() >= deadline:
                typer.echo(
                    "  ⓘ Timed out waiting for the install. Finish it on GitHub, then run "
                    "`loopy auth status` to verify."
                )
                return None
            sleep(poll_interval)
    except KeyboardInterrupt:
        typer.echo(
            "\n  (skipped — App created but not installed yet. Install it, then run "
            "`loopy auth status`.)"
        )
        return None


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


def _configured_public_url(root: str | Path) -> str | None:
    """The public base URL this project is configured with, if any.

    `LOOPY_PUBLIC_URL` in the process env wins — it's a deploy-time constant, set once
    where the server runs — else the project's registry.yml `public_url`. The registry
    read is deliberately lenient (the compiler owns strict validation, E216); auth only
    needs to know whether a URL exists.
    """
    env = os.environ.get("LOOPY_PUBLIC_URL", "").strip()
    if env:
        return env
    path = Path(root) / "registry.yml"
    if not path.is_file():
        return None
    try:
        from ruamel.yaml import YAML

        data = YAML(typ="safe").load(path.read_text())
    except Exception:  # noqa: BLE001 - a broken registry fails at compile, not here
        return None
    if not isinstance(data, Mapping):
        return None
    value = data.get("public_url")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _resolve_public_url(root: str | Path, explicit: str | None) -> str | None:
    """Resolve and normalize the public base URL, or None when the project has none.

    Precedence: an explicit value (a `--public-url` flag, or the init wizard's
    just-collected answer) > the configured sources (`LOOPY_PUBLIC_URL` env,
    registry.yml `public_url`). A malformed URL exits with an error rather than being
    baked into a GitHub App, where it would surface as silent non-delivery much later.
    """
    from loopy_core.registry.loader import normalize_public_url

    raw = explicit if explicit and explicit.strip() else _configured_public_url(root)
    if not raw:
        return None
    try:
        return normalize_public_url(raw)
    except ValueError as exc:
        typer.echo(f"error: invalid public URL: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def run_github_auth(
    *,
    org: str | None = None,
    name: str | None = None,
    port: int = DEFAULT_PORT,
    root: Path = Path("."),
    force: bool = False,
    no_browser: bool = False,
    public_url: str | None = None,
    no_webhook: bool = False,
) -> None:
    """Create a GitHub App via the manifest flow and store its credentials.

    The plain-function core of `loopy auth github`, callable in-process (e.g. from the
    `loopy init` wizard) without going through Typer's argument parsing — calling the
    decorated command directly would pass `OptionInfo` sentinels instead of real values.

    When the project has a public URL — `public_url` here (the wizard passes the answer it
    just collected), else `LOOPY_PUBLIC_URL` in the env, else registry.yml `public_url` —
    the App is created *with* its webhook at `<public-url>/hooks/github`, and the webhook
    secret GitHub mints lands in loopy.env as `GITHUB_WEBHOOK_SECRET`. Creation is the
    only moment GitHub hands us that secret, so this is when webhooks become zero-setup.
    With no URL (or `no_webhook`), the App is created webhook-less, a pure credential
    source, exactly as before.
    """
    from loopy_runtime.scm import github_app
    from loopy_runtime.secrets import load_control_plane_env

    if not force and load_control_plane_env(root).get("GITHUB_APP_ID"):
        typer.echo(
            "error: GITHUB_APP_ID already set in loopy.env — re-run with --force to overwrite.",
            err=True,
        )
        raise typer.Exit(code=1)

    webhook_url: str | None = None
    if not no_webhook:
        base = _resolve_public_url(root, public_url)
        if base:
            webhook_url = base + GITHUB_WEBHOOK_PATH

    app_name = name or default_app_name(org)
    typer.echo(typer.style("\n  🔐  loopy auth github", fg=typer.colors.CYAN, bold=True))
    typer.echo(typer.style("  " + "─" * 40, fg=typer.colors.BRIGHT_BLACK))
    where = f"org '{org}'" if org else "your personal account"
    typer.echo(f"  → creating GitHub App '{app_name}' under {where}")
    if webhook_url:
        typer.echo(f"  → registering its webhook at {webhook_url} (--no-webhook to skip)")

    code = obtain_manifest_code(
        name=app_name, org=org, port=port, open_browser=not no_browser, webhook_url=webhook_url
    )
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
    if webhook_url:
        if conversion.get("webhook_secret"):
            typer.echo(
                typer.style("  ✓", fg=typer.colors.GREEN)
                + f" webhook registered at {webhook_url} — GITHUB_WEBHOOK_SECRET stored, "
                "deliveries will be signature-verified"
            )
        else:
            typer.echo(
                "  ⓘ GitHub returned no webhook secret — run `loopy auth webhook` to "
                "re-point the webhook with a fresh secret"
            )

    slug = conversion.get("slug")
    if slug:
        install_url = f"https://github.com/apps/{slug}/installations/new"
        typer.echo("\n  Next: install the App on the repos loopy should access:")
        typer.echo(typer.style(f"    {install_url}", fg=typer.colors.BLUE))
        if not no_browser:
            import webbrowser

            # Announce the open the way the create-app step does — webbrowser.open is
            # silent (and a no-op headless/over SSH), so without this it looks like we
            # only printed the link.
            typer.echo("  → opening the install page in your browser…")
            webbrowser.open(install_url)
        else:
            typer.echo("  → --no-browser: open the URL above to install")
        # Creating the App did not install it. Block until the install lands rather than
        # hand back a created-but-uninstalled App that fails much later at `loopy run`.
        _wait_for_installation(root)

    typer.echo("\n  Verifying credentials…")
    _verify(root)
    typer.echo()


@auth_app.command()
def github(
    org: str | None = typer.Option(None, "--org", help="Create under this org (default: account)."),
    name: str | None = typer.Option(None, "--name", help="App name (default: loopy[-org])."),
    port: int = typer.Option(DEFAULT_PORT, "--port", help="Local callback port (0 = ephemeral)."),
    root: Path = typer.Option(Path("."), "--root", help="Project root (where loopy.env lives)."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing stored App credentials."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the URL, don't open it."),
    public_url: str | None = typer.Option(
        None,
        "--public-url",
        help="Public base URL of this loopy server; the App's webhook is registered at "
        "<url>/hooks/github (default: LOOPY_PUBLIC_URL, else registry.yml public_url).",
    ),
    no_webhook: bool = typer.Option(
        False,
        "--no-webhook",
        help="Create the App without a webhook even when a public URL is configured.",
    ),
) -> None:
    """Create your own GitHub App via the manifest flow and store its credentials."""
    run_github_auth(
        org=org,
        name=name,
        port=port,
        root=root,
        force=force,
        no_browser=no_browser,
        public_url=public_url,
        no_webhook=no_webhook,
    )


def app_settings_url(app_info: Mapping) -> str:
    """The App's settings page — where the webhook's Active flag and event subscriptions live."""
    slug = app_info.get("slug", "")
    owner = app_info.get("owner") or {}
    if owner.get("type") == "Organization":
        return f"https://github.com/organizations/{owner.get('login')}/settings/apps/{slug}"
    return f"https://github.com/settings/apps/{slug}"


def run_webhook_setup(*, root: Path = Path("."), public_url: str | None = None) -> None:
    """Point the already-created App's webhook at the project's public URL (the retrofit path).

    `loopy auth github` registers the webhook at App *creation* when a public URL is already
    known — the only moment GitHub mints the secret for us. This covers the other order: the
    App exists, the URL came later. It generates a secret locally, PATCHes the App's webhook
    config to `<public-url>/hooks/github` with it, and stores it in loopy.env — so URL and
    secret need no hand-copying. What the API *can't* do is flip the webhook's Active flag or
    subscribe the App to events (App-settings-only), so the command ends by pointing at the
    exact settings page when those still need a click.
    """
    from loopy_runtime.scm import github_app
    from loopy_runtime.secrets import (
        CONTROL_PLANE_ENV_FILE,
        load_control_plane_env,
        write_control_plane_env,
    )

    base = _resolve_public_url(root, public_url)
    if not base:
        typer.echo(
            "error: no public URL configured — set `public_url` in registry.yml (or "
            "LOOPY_PUBLIC_URL / --public-url) so there's an address to register.",
            err=True,
        )
        raise typer.Exit(code=1)
    desired = base + GITHUB_WEBHOOK_PATH

    try:
        creds = _load_creds(root)
    except github_app.MissingCredentials as exc:
        typer.echo(f"error: no GitHub App configured ({exc}) — run `loopy auth github` first.")
        raise typer.Exit(code=1) from exc

    typer.echo(typer.style("\n  🔗  loopy auth webhook", fg=typer.colors.CYAN, bold=True))
    typer.echo(typer.style("  " + "─" * 40, fg=typer.colors.BRIGHT_BLACK))

    try:
        current = github_app.get_webhook_config(creds)
    except github_app.GitHubAppError as exc:
        typer.echo(f"error: couldn't read the App's webhook config: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if current.get("url") == desired and load_control_plane_env(root).get(
        "GITHUB_WEBHOOK_SECRET"
    ):
        typer.echo(
            typer.style("  ✓", fg=typer.colors.GREEN)
            + f" webhook already points at {desired} and a secret is stored — nothing to do"
        )
    else:
        # GitHub only hands out a secret at App creation; on the retrofit path we mint our
        # own and set both sides of it (the App's config and loopy.env) in one motion.
        secret = secrets.token_hex(32)
        try:
            github_app.update_webhook_config(creds, url=desired, secret=secret)
        except github_app.GitHubAppError as exc:
            typer.echo(f"error: couldn't update the App's webhook config: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        env_path = write_control_plane_env(root, {"GITHUB_WEBHOOK_SECRET": secret})
        _ensure_gitignored(Path(root), CONTROL_PLANE_ENV_FILE)
        typer.echo(
            typer.style("  ✓", fg=typer.colors.GREEN)
            + f" webhook pointed at {desired} — new secret stored in {env_path} (gitignored)"
        )

    # The API stops here: Active + event subscriptions are settings-page-only. Check what
    # we can see (the subscribed events ride the App record) and say exactly what's left.
    try:
        app_info = github_app.get_app(creds)
    except github_app.GitHubAppError as exc:
        typer.echo(f"  (couldn't verify event subscriptions: {exc})")
        return
    settings = app_settings_url(app_info)
    events = app_info.get("events") or []
    if events:
        typer.echo(f"  subscribed events: {', '.join(sorted(events))}")
        typer.echo(
            typer.style(
                f"  If deliveries don't arrive, confirm the webhook is Active: {settings}",
                fg=typer.colors.BRIGHT_BLACK,
            )
        )
    else:
        typer.echo(
            "  ⚠ the App subscribes to no events yet, so GitHub will deliver nothing."
        )
        typer.echo("    Finish in the App's settings (no API exists for these two):")
        typer.echo(typer.style(f"    {settings}", fg=typer.colors.BLUE))
        typer.echo(
            "    → check 'Active' under Webhook, and pick events under 'Permissions & "
            "events' (Pull requests, Issues, Issue comments, Pushes cover the built-ins)"
        )
    typer.echo()


@auth_app.command()
def webhook(
    root: Path = typer.Option(Path("."), "--root", help="Project root (where loopy.env lives)."),
    public_url: str | None = typer.Option(
        None,
        "--public-url",
        help="Public base URL to register (default: LOOPY_PUBLIC_URL, else registry.yml "
        "public_url).",
    ),
) -> None:
    """Point the configured GitHub App's webhook at this project's public URL."""
    run_webhook_setup(root=root, public_url=public_url)


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
