"""`loopy auth github` — one-command GitHub App onboarding via the manifest flow.

Walks a self-hosting user through creating *their own* GitHub App and lands its
credentials locally, so loopy can later mint short-lived, repo-scoped tokens for
agents — no loopy-owned central app, no persistent server.

The flow:
  1. build a manifest (minimal perms, no webhook) pointing at a local callback;
  2. serve a one-shot 127.0.0.1 listener (the `gh auth login` pattern);
  3. open the browser to a local page that auto-POSTs the manifest to GitHub;
  4. GitHub redirects back with a temporary `?code=`; exchange it for creds;
  5. persist the App id + private key (inline, gitignored) into `loopy.env`;
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
    """Assemble the GitHub App manifest: minimal fix/PR permissions, no App webhook.

    `hook_attributes` is deliberately omitted: GitHub requires `hook_attributes.url`
    whenever the object is present (sending `{active: false}` alone fails with
    "url wasn't supplied"), and an App webhook's event subscriptions have no update
    API — it could only ever be wired at creation. Event delivery is *repo* webhooks
    instead (`loopy webhooks github`), which work at any time; the `repository_hooks`
    permission below is what lets the App create them.
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
            # create/update the repo webhooks that deliver built-in Github.* events
            "repository_hooks": "write",
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


# After the manifest flow *creates* the App, installing it on repos is a separate manual
# step on GitHub. Poll this often, for at most this long, for the install to land before
# giving up and pointing the user at `loopy auth github` to finish later.
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
                    "`loopy auth github` to verify."
                )
                return None
            sleep(poll_interval)
    except KeyboardInterrupt:
        typer.echo(
            "\n  (skipped — App created but not installed yet. Install it, then run "
            "`loopy auth github`.)"
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


def run_github_auth(
    *,
    org: str | None = None,
    name: str | None = None,
    port: int = DEFAULT_PORT,
    root: Path = Path("."),
    force: bool = False,
    no_browser: bool = False,
) -> None:
    """Create a GitHub App via the manifest flow and store its credentials.

    The plain-function core of `loopy auth github`, callable in-process (e.g. from the
    `loopy init` wizard) without going through Typer's argument parsing — calling the
    decorated command directly would pass `OptionInfo` sentinels instead of real values.
    """
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

    # Event delivery is wired separately, on purpose: built-in `Github.*` triggers only fire
    # once GitHub can deliver to the engine, and that's the one explicit `loopy webhooks
    # github` step. Auth doesn't do it inline — the public URL may not exist yet (a
    # provisioned host mints it at deploy). Just point at the next step.
    typer.echo(
        typer.style(
            "  Next: `loopy webhooks github` to register event delivery "
            "(needs LOOPY_PUBLIC_URL).",
            fg=typer.colors.BRIGHT_BLACK,
        )
    )


def _github_status(root: str | Path) -> None:
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
    typer.echo(
        typer.style(
            "  Re-run with --force to create a new App and overwrite these credentials.",
            fg=typer.colors.BRIGHT_BLACK,
        )
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
    """Show the GitHub App status if one is registered, else create it via the manifest flow.

    `loopy auth github` shows and verifies the stored GitHub App when credentials already
    exist; otherwise it runs the manifest flow to create one. Pass --force to re-run the
    flow and overwrite existing credentials.
    """
    from loopy_runtime.secrets import load_control_plane_env

    if not force and load_control_plane_env(root).get("GITHUB_APP_ID"):
        _github_status(root)
        return
    run_github_auth(org=org, name=name, port=port, root=root, force=force, no_browser=no_browser)


# ── Sentry: create a Custom Integration so `Sentry.*` built-ins have a producer ──────────
#
# Unlike GitHub (a browser manifest flow that mints an App from nothing), Sentry has no
# create-without-credentials path for internal integrations, so this takes a bootstrap auth
# token — read from the environment, else prompted — and calls the API directly. The token
# only creates the integration and is never stored; what we persist is the integration's
# Client Secret (`SENTRY_WEBHOOK_SECRET`, which signs inbound webhooks) and its slug.

SENTRY_DEFAULT_BASE = "https://sentry.io"
SENTRY_HOOK_PATH = "/hooks/sentry"
# The token needs org-write to create an integration; a CI Organization Auth Token doesn't.
_SENTRY_TOKEN_HELP = (
    "Get one in Sentry: Settings -> Developer Settings -> Personal Tokens -> Create New "
    "Token, scope 'org:write'.\n"
    "  (Not the sibling 'Organization Tokens': those are CI-scoped and can't create "
    "integrations.)"
)


def _project_env(root: str | Path) -> dict[str, str]:
    """Process env with `loopy.env` merged underneath (process env wins)."""
    from loopy_runtime.secrets import load_control_plane_env

    merged = dict(os.environ)
    for key, value in load_control_plane_env(root).items():
        merged.setdefault(key, value)
    return merged


def _sentry_webhook_url(flag: str | None, env: dict[str, str]) -> str:
    """Resolve the public webhook URL: --webhook-url -> $LOOPY_PUBLIC_URL -> prompt.

    The public base is shared, provider-agnostic config (`loopy_runtime.config`); here we
    just append `/hooks/sentry` and check it's reachable. Sentry must reach it over public
    HTTPS, so a localhost/non-https answer is warned about (not fatal — the user may tunnel)."""
    import urllib.parse

    from loopy_runtime import config

    base = config.resolve_public_url(flag, env=env)
    if not base:
        typer.echo(
            "\n  Sentry delivers webhooks to a public HTTPS URL. In local dev, expose "
            "`loopy run`\n  with a tunnel (e.g. `cloudflared tunnel --url http://127.0.0.1:8000`)."
        )
        base = typer.prompt("  Public base URL (or full /hooks/sentry URL)").strip()
    parts = urllib.parse.urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise typer.BadParameter(f"webhook URL must be an absolute http(s) URL, got {base!r}")
    if parts.scheme != "https" or parts.hostname in ("localhost", "127.0.0.1"):
        typer.echo(
            typer.style("  warning:", fg=typer.colors.YELLOW)
            + f" {base} isn't public HTTPS; Sentry won't reach it until you tunnel/deploy."
        )
    return config.hook_url(base, SENTRY_HOOK_PATH)


def _resolve_sentry_org(token: str, org: str | None, base_url: str, env: dict[str, str]) -> str:
    """--org -> $SENTRY_ORG -> auto-detect (one org: use it; several: ask)."""
    from loopy_runtime.scm import sentry_app

    if org or env.get("SENTRY_ORG"):
        return org or env["SENTRY_ORG"]
    orgs = sentry_app.list_organizations(token, base_url=base_url)
    if len(orgs) == 1:
        return orgs[0]["slug"]
    slugs = ", ".join(o.get("slug", "?") for o in orgs) or "(none)"
    raise typer.BadParameter(
        f"could not pick an org automatically; pass --org. The token sees: {slugs}"
    )


def write_sentry_credentials(root: str | Path, *, secret: str, slug: str | None) -> Path:
    """Persist the Client Secret (+ slug, if API-created) into gitignored loopy.env."""
    from loopy_runtime.secrets import CONTROL_PLANE_ENV_FILE, write_control_plane_env

    updates = {"SENTRY_WEBHOOK_SECRET": secret}
    if slug:
        updates["SENTRY_APP_SLUG"] = slug
    env_path = write_control_plane_env(root, updates)
    _ensure_gitignored(root, CONTROL_PLANE_ENV_FILE)
    return env_path


def run_sentry_auth(
    *,
    org: str | None = None,
    webhook_url: str | None = None,
    name: str = "loopy",
    events: str = "issue",
    sentry_url: str | None = None,
    root: Path = Path("."),
    force: bool = False,
    manual: bool = False,
    update: bool = False,
) -> None:
    """Create (or update) a Sentry Custom Integration and store its Client Secret."""
    from loopy_runtime.scm import sentry_app
    from loopy_runtime.secrets import load_control_plane_env

    env = _project_env(root)
    base_url = sentry_url or env.get("SENTRY_URL") or SENTRY_DEFAULT_BASE
    stored = load_control_plane_env(root)

    typer.echo(typer.style("\n  🔐  loopy auth sentry", fg=typer.colors.CYAN, bold=True))
    typer.echo(typer.style("  " + "─" * 40, fg=typer.colors.BRIGHT_BLACK))

    # --manual: the API isn't usable (e.g. only a CI token); store a pasted secret.
    if manual:
        secret = typer.prompt("  Paste the integration's Client Secret", hide_input=True).strip()
        env_path = write_sentry_credentials(root, secret=secret, slug=None)
        typer.echo(
            typer.style("  ✓", fg=typer.colors.GREEN)
            + f" wrote SENTRY_WEBHOOK_SECRET to {env_path}"
        )
        return

    token = env.get("SENTRY_AUTH_TOKEN")
    if not token:
        typer.echo("  " + _SENTRY_TOKEN_HELP)
        token = typer.prompt("  Sentry auth token", hide_input=True).strip()
    if not token:
        typer.echo("error: no Sentry auth token provided", err=True)
        raise typer.Exit(code=1)

    # --update: repoint the existing integration's webhook URL; leave the secret alone.
    if update:
        slug = stored.get("SENTRY_APP_SLUG")
        if not slug:
            typer.echo(
                "error: no SENTRY_APP_SLUG in loopy.env; run `loopy auth sentry` first.", err=True
            )
            raise typer.Exit(code=1)
        url = _sentry_webhook_url(webhook_url, env)
        try:
            sentry_app.update_integration_webhook(token, slug, url, base_url=base_url)
        except sentry_app.SentryAPIError as exc:
            _fail_sentry(exc)
        typer.echo(
            typer.style("  ✓", fg=typer.colors.GREEN) + f" updated {slug} webhook URL to {url}"
        )
        return

    if stored.get("SENTRY_WEBHOOK_SECRET") and not force:
        typer.echo(
            "error: SENTRY_WEBHOOK_SECRET already set in loopy.env; "
            "re-run with --force to overwrite.",
            err=True,
        )
        raise typer.Exit(code=1)

    org = _resolve_sentry_org(token, org, base_url, env)
    url = _sentry_webhook_url(webhook_url, env)
    event_list = [e.strip() for e in events.split(",") if e.strip()]
    typer.echo(f"  → creating internal integration '{name}' in org '{org}'")
    try:
        app = sentry_app.create_internal_integration(
            token,
            org,
            name=name,
            webhook_url=url,
            events=event_list,
            scopes=["event:read"],
            base_url=base_url,
        )
    except sentry_app.SentryAPIError as exc:
        _fail_sentry(exc)

    secret = app.get("clientSecret")
    slug = app.get("slug")
    if not secret:
        typer.echo(
            "error: Sentry didn't return a Client Secret. Copy it from the integration in the "
            "UI\n  and run `loopy auth sentry --manual`.",
            err=True,
        )
        raise typer.Exit(code=1)
    # Write immediately: the secret is shown only in this response and masked on every read after.
    env_path = write_sentry_credentials(root, secret=secret, slug=slug)
    typer.echo(
        typer.style("  ✓", fg=typer.colors.GREEN)
        + f" created '{slug}'; wrote SENTRY_WEBHOOK_SECRET + SENTRY_APP_SLUG to {env_path}"
    )
    typer.echo(
        "\n  Next: start `loopy run`, then trigger a test issue in Sentry to confirm delivery."
        "\n  (For Sentry.AlertTriggered, also add this integration as an Alert Rule Action.)\n"
    )


def _fail_sentry(exc) -> None:  # -> NoReturn
    """Turn a Sentry API error into a clean CLI exit, with scope guidance on a 403."""
    if getattr(exc, "status", None) == 403:
        typer.echo(
            "error: the token can't create an integration (403). Use a Personal Token (Settings\n"
            "  -> Developer Settings -> Personal Tokens) or an internal-integration token with\n"
            "  'org:write' — the sibling 'Organization Tokens' type is CI-scoped and won't work.\n"
            "  Or run `loopy auth sentry --manual`.",
            err=True,
        )
    else:
        typer.echo(f"error: {exc}", err=True)
    raise typer.Exit(code=1)


def _sentry_status(root: str | Path) -> None:
    """Show the stored Sentry Custom Integration credentials."""
    from loopy_runtime.secrets import load_control_plane_env

    slug = load_control_plane_env(root).get("SENTRY_APP_SLUG")
    detail = f" '{slug}'" if slug else ""
    typer.echo(
        typer.style("  ✓", fg=typer.colors.GREEN)
        + f" Sentry integration{detail} configured (SENTRY_WEBHOOK_SECRET set)"
    )
    typer.echo(
        typer.style(
            "  Re-run with --force to recreate, --update to repoint its webhook URL.",
            fg=typer.colors.BRIGHT_BLACK,
        )
    )


@auth_app.command()
def sentry(
    org: str | None = typer.Option(
        None, "--org", help="Sentry org slug (default: $SENTRY_ORG, else auto-detect)."
    ),
    webhook_url: str | None = typer.Option(
        None, "--webhook-url", help="Public URL; default $LOOPY_PUBLIC_URL + /hooks/sentry."
    ),
    name: str = typer.Option("loopy", "--name", help="Integration name."),
    events: str = typer.Option("issue", "--events", help="Comma-separated resources to subscribe."),
    sentry_url: str | None = typer.Option(
        None, "--sentry-url", help="Base URL for self-hosted Sentry ($SENTRY_URL)."
    ),
    root: Path = typer.Option(Path("."), "--root", help="Project root (where loopy.env lives)."),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing SENTRY_WEBHOOK_SECRET."
    ),
    manual: bool = typer.Option(
        False, "--manual", help="Skip the API; paste a Client Secret from the UI."
    ),
    update: bool = typer.Option(
        False, "--update", help="Repoint the stored integration's webhook URL."
    ),
) -> None:
    """Show the Sentry integration status if one is registered, else create it.

    When a Client Secret is already stored, `loopy auth sentry` prints its status;
    otherwise it creates a Custom Integration for the built-in `Sentry.*` events. Reads the
    bootstrap token from $SENTRY_AUTH_TOKEN (else prompts). The token creates the integration
    and is not stored; only the Client Secret (SENTRY_WEBHOOK_SECRET) is. Pass --force to
    recreate, --update to repoint the webhook URL, or --manual to paste a secret.
    """
    from loopy_runtime.secrets import load_control_plane_env

    if (
        not (force or manual or update)
        and load_control_plane_env(root).get("SENTRY_WEBHOOK_SECRET")
    ):
        _sentry_status(root)
        return
    run_sentry_auth(
        org=org,
        webhook_url=webhook_url,
        name=name,
        events=events,
        sentry_url=sentry_url,
        root=root,
        force=force,
        manual=manual,
        update=update,
    )
