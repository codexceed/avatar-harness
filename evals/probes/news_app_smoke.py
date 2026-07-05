"""Functional success probe for `news-analyzer` — the full app must round-trip, ops included.

Usage: ``python news_app_smoke.py <entry_file>`` with the scratch repo as cwd. The entry file
is named by the task, so there is no discovery guesswork. The probe stands up ONE local stub
server playing both external APIs — ``GET /v4/search`` returns canned gnews-shaped article
lists (different per query); ``POST …/chat/completions`` returns a canned per-article
analysis (a JSON object with ``summary`` + ``sentiment``, matched to the article in the
request) — then exercises the app across four launches plus a static check:

    docs  config documented with the app files (README / app.py docstring or comments)
    A     launched WITHOUT NEWS_API_URL, then WITHOUT NEWS_API_KEY -> each must exit
                                               non-zero fast, naming the missing variable
    B     working stubs                     -> the UI + API gauntlet: two search queries,
                                               a per-article analyze form on every result,
                                               three analyses (two via the UI form, one via
                                               the API), distinct summaries + sentiments
                                               rendered, malformed API body -> 4xx
    C     NEWS_API_URL -> non-JSON endpoint -> /search renders a legible HTML error naming
                                               the news API; the server stays up
    D     restart on a fresh port           -> all stored analyses persist (real db)

Strict by design (mirrors chatbot_smoke): every step needs positive evidence — an app that
"looks right" but never calls the model, hardcodes one summary, stores in memory only,
renders nothing, silently swallows missing config, or serves a working API with no operable
UI fails. The news stub requires the ``apikey`` query parameter (= NEWS_API_KEY), like a
real news API, so an app that never sends the key cannot search. The chat stub must have
OBSERVED one chat/completions call per analysis, each carrying its article (the per-article
reply is keyed on the request body), so a canned or reused analysis cannot pass. UI
structure is checked with quoting-agnostic regexes over the served HTML, then *exercised*
by submitting the same requests a browser would.

Exit codes: 0 = every check passed; 1 = a check failed (reason printed).
"""

import ast
import contextlib
import html
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# One row per (query, article): the stub serves the articles per query, and replies to a
# chat call with the summary/sentiment of whichever article appears in the request body —
# so every analysis must genuinely carry its article to the model.
_CASES = [
    {
        "query": "fusion",
        "title": "Aries pilot plant hits fusion milestone",
        "url": "https://news.example.com/aries-fusion",
        "description": "The pilot plant sustained a burning plasma for a record duration.",
        "content": "Engineers at the Aries pilot plant reported a sustained burning plasma, "
        "a key milestone on the road to net-positive fusion power.",
        "summary": "Aries pilot plant sustained a burning plasma, a key step toward net-positive fusion.",
        "sentiment": "positive",
    },
    {
        "query": "fusion",
        "title": "Tokamak startup delays reactor after magnet setback",
        "url": "https://news.example.com/tokamak-delay",
        "description": "A superconducting magnet failure pushes the demo reactor back a year.",
        "content": "The startup said a failed superconducting magnet coil will delay its "
        "demonstration reactor by at least a year, unsettling investors.",
        "summary": "A superconducting magnet failure delays the startup's demo reactor by a year.",
        "sentiment": "negative",
    },
    {
        "query": "chips",
        "title": "Fab yields level off on the new lithography node",
        "url": "https://news.example.com/fab-yields",
        "description": "Yields on the extreme-ultraviolet node have flattened, matching forecasts.",
        "content": "Foundry data shows yields on the new extreme-ultraviolet lithography node "
        "leveling off in line with projections, neither beating nor missing them.",
        "summary": "Yields on the new extreme-ultraviolet node flattened, matching forecasts.",
        "sentiment": "neutral",
    },
]
_CONFIG_NAMES = ("PORT", "NEWS_API_URL", "NEWS_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL")
_NEWS_API_KEY = "stub-news-key-7f3a"  # the stub requires this apikey, like a real news API
_START_DEADLINE_SECONDS = 20.0
_FAIL_FAST_DEADLINE_SECONDS = 10.0
_chat_calls: list[str] = []


def _article_payload(case: dict) -> dict:
    """The gnews-shaped article object for one case row."""
    return {k: case[k] for k in ("title", "url", "description", "content")}


class _StubHandler(BaseHTTPRequestHandler):
    """Both external APIs on one server, plus a deliberately broken news route."""

    def log_message(self, format: str, *args: object) -> None:
        """Silence per-request logging (stdlib override signature)."""

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: object) -> None:
        self._send(200, json.dumps(payload).encode("utf-8"), "application/json")

    def do_GET(self) -> None:
        """News search endpoint (gnews-shaped, per-query articles) + a broken route."""
        parts = urllib.parse.urlparse(self.path)
        if parts.path.startswith("/v4/search"):
            params = urllib.parse.parse_qs(parts.query)
            # Like real news APIs (gnews): no valid apikey, no articles. An app that never
            # sends NEWS_API_KEY cannot pass the search steps.
            if params.get("apikey", [""])[0] != _NEWS_API_KEY:
                body = json.dumps({"errors": ["You did not provide a valid API key."]})
                self._send(401, body.encode("utf-8"), "application/json")
                return
            q = params.get("q", [""])[0].strip().lower()
            articles = [_article_payload(c) for c in _CASES if c["query"] == q]
            self._send_json({"articles": articles})
        elif parts.path.startswith("/broken"):
            # A news API gone bad: 200 with a non-JSON body (launch C).
            self._send(200, b"<html><body>scheduled maintenance</body></html>", "text/html")
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        """OpenAI-compatible chat completions, replying per the article in the request."""
        if "chat/completions" not in self.path:
            self.send_error(404)
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)).decode(
            "utf-8", errors="replace"
        )
        _chat_calls.append(self.path)
        matched = next(
            (c for c in _CASES if c["title"] in raw or c["content"][:40] in raw),
            None,
        )
        if matched is None:
            reply = {
                "summary": "UNMATCHED ARTICLE — the request carried no known article",
                "sentiment": "neutral",
            }
        else:
            reply = {"summary": matched["summary"], "sentiment": matched["sentiment"]}
        self._send_json(
            {
                "id": "chatcmpl-eval-stub",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-4.1-nano",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": json.dumps(reply)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )


def _free_port() -> int:
    """Reserve an ephemeral port and release it for the app to bind."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_port(port: int, deadline_seconds: float) -> bool:
    """Poll until 127.0.0.1:`port` accepts a connection or the deadline passes."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.25)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def _request(url: str, payload: dict | None = None, *, form: bool = False) -> tuple[int, str]:
    """GET `url` (or POST `payload`), returning (status, body) without raising.

    `form=True` posts `payload` form-encoded — the same request a browser form submit
    sends — instead of JSON; redirects are followed either way.
    """
    if payload is None:
        data, headers = None, {}
    elif form:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
    else:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers)  # noqa: S310 — probe-built http://127.0.0.1 URLs only
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 — localhost stub/app
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        return 0, f"(request failed: {exc})"


def _ui_request(url: str, payload: dict | None = None, *, form: bool = False) -> tuple[int, str]:
    """`_request`, with the body HTML-unescaped for substring checks against page content.

    An app that correctly `html.escape`s what it renders turns an apostrophe into
    ``&#x27;`` — a plain substring check would punish exactly the well-behaved apps
    (the 2026-07-04 matrix run failed 12/16 cells on this). Unescaping first makes the
    content checks encoding-agnostic; tag-structure regexes are unaffected.
    """
    status, body = _request(url, payload, form=form)
    return status, html.unescape(body)


def _has_form(page: str, action: str, input_name: str | None = None) -> bool:
    """Whether `page` contains a `<form>` targeting `action`, quoting/attribute-order agnostic.

    When `input_name` is given, an `<input>`/`<textarea>` of that name must also appear
    somewhere on the page.
    """
    if not re.search(rf"<form[^>]*action\s*=\s*[\"']?{re.escape(action)}", page, re.IGNORECASE):
        return False
    if input_name is None:
        return True
    return bool(
        re.search(
            rf"<(?:input|textarea)[^>]*name\s*=\s*[\"']?{re.escape(input_name)}[\"'\s/>]",
            page,
            re.IGNORECASE,
        )
    )


def _count_forms(page: str, action: str) -> int:
    """How many `<form>`s in `page` target `action`."""
    return len(re.findall(rf"<form[^>]*action\s*=\s*[\"']?{re.escape(action)}", page, re.IGNORECASE))


def _launch_app(
    script: Path,
    port: int,
    stub_port: int,
    *,
    news_path: str = "/v4/search",
    omit: str | None = None,
) -> subprocess.Popen[str]:
    """Start the app under test with the stub endpoints wired in via env.

    `news_path` is the news endpoint on the stub server (`/broken` for the degraded
    launch). `omit` drops that one env var entirely (the fail-fast launches).
    """
    env = {k: v for k, v in os.environ.items() if k not in ("NEWS_API_URL", "NEWS_API_KEY")}
    env.update(
        {
            "PORT": str(port),
            "NEWS_API_URL": f"http://127.0.0.1:{stub_port}{news_path}",
            "NEWS_API_KEY": _NEWS_API_KEY,
            "OPENAI_BASE_URL": f"http://127.0.0.1:{stub_port}/v1",
        }
    )
    env.setdefault("OPENAI_API_KEY", "sk-eval-dummy")
    if omit is not None:
        env.pop(omit, None)
    return subprocess.Popen(
        [sys.executable, str(script)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _stop_app(proc: subprocess.Popen[str]) -> str:
    """Terminate the app, returning a combined output tail for diagnostics."""
    proc.terminate()
    try:
        out, err = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    return ((out or "") + (err or "")).strip()[-800:]


# --------------------------------------------------------------------------- #
# The checks, grouped by launch
# --------------------------------------------------------------------------- #
def _docs_check(script: Path) -> str | None:
    """Static: every pinned config name is documented with the app files.

    Documentation surfaces: README/*.md/*.txt/*.rst at the repo root, plus `app.py`'s
    module docstring and comment lines. A bare `os.environ[...]` read in code is not
    documentation and does not count.
    """
    surfaces: list[str] = []
    for pattern in ("*.md", "*.txt", "*.rst"):
        for doc in Path.cwd().glob(pattern):
            try:
                surfaces.append(doc.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    source = script.read_text(encoding="utf-8", errors="replace")
    with contextlib.suppress(SyntaxError):
        surfaces.append(ast.get_docstring(ast.parse(source)) or "")
    surfaces.extend(line for line in source.splitlines() if line.lstrip().startswith("#"))
    doc_text = "\n".join(surfaces)
    missing = [name for name in _CONFIG_NAMES if name not in doc_text]
    if missing:
        return (
            f"config not documented: {', '.join(missing)} never appear in a README or in "
            f"{script.name}'s docstring/comments (code reads alone are not documentation)"
        )
    return None


def _fail_fast_check(script: Path, stub_port: int) -> str | None:
    """Launch A: missing required config (either var) must exit non-zero fast, named."""
    for var in ("NEWS_API_URL", "NEWS_API_KEY"):
        proc = _launch_app(script, _free_port(), stub_port, omit=var)
        try:
            out, err = proc.communicate(timeout=_FAIL_FAST_DEADLINE_SECONDS)
        except subprocess.TimeoutExpired:
            _stop_app(proc)
            return (
                f"launched without {var} the app kept running — it must exit non-zero "
                "at startup with an error naming the missing variable"
            )
        if proc.returncode == 0:
            return f"launched without {var} the app exited 0 — required config must be an error"
        combined = (out or "") + (err or "")
        if var not in combined:
            return f"the startup error does not name {var}; output: {combined.strip()[:300]}"
    return None


def _functional_checks(base: str) -> str | None:  # noqa: PLR0911, PLR0912, C901 — a flat step gauntlet; each failure returns its reason
    """Launch B: the UI + API gauntlet against working stubs."""
    fusion, delay, chips = _CASES

    # -- UI, driven as a human in a browser would --------------------------------
    status, body = _ui_request(f"{base}/")
    if status != 200 or not _has_form(body, "/search", input_name="q"):
        return (
            f"home page failed (status {status}): no search form targeting /search "
            f"with an input named q; body: {body[:300]}"
        )

    status, body = _ui_request(f"{base}/search?q=fusion")
    if status != 200 or fusion["title"] not in body or delay["title"] not in body:
        return (
            f"search page (q=fusion) failed (status {status}): both article titles must "
            f"be rendered; body: {body[:300]}"
        )
    if _count_forms(body, "/analyze") < 2:
        return (
            "search page (q=fusion) must offer an analyze form/button per result "
            f"(2 articles, {_count_forms(body, '/analyze')} analyze form(s) found)"
        )

    status, body = _ui_request(f"{base}/search?q=chips")
    if status != 200 or chips["title"] not in body or not _has_form(body, "/analyze"):
        return (
            f"search page (q=chips) failed (status {status}): second query's article + "
            f"analyze form must render; body: {body[:300]}"
        )

    for case in (fusion, delay):  # two UI analyses — one per result button
        fields = {k: case[k] for k in ("title", "url", "content")}
        status, body = _ui_request(f"{base}/analyze", fields, form=True)
        if status not in (200, 201):
            return (
                f"analyze form submit for '{case['title'][:40]}…' failed "
                f"(status {status}); body: {body[:300]}"
            )

    status, body = _ui_request(f"{base}/")
    for case in (fusion, delay):
        if case["summary"] not in body or case["sentiment"] not in body:
            return (
                f"display step failed (status {status}): summary+sentiment for "
                f"'{case['title'][:40]}…' not rendered on the home page; body: {body[:300]}"
            )

    # -- the REST API, driven as a client ----------------------------------------
    status, body = _request(f"{base}/api/articles?q=fusion")
    if status != 200 or fusion["title"] not in body or delay["title"] not in body:
        return f"API search (q=fusion) failed (status {status}): both articles required; body: {body[:300]}"

    status, body = _request(f"{base}/api/articles?q=chips")
    if status != 200 or chips["title"] not in body:
        return f"API search (q=chips) failed (status {status}); body: {body[:300]}"

    fields = {k: chips[k] for k in ("title", "url", "content")}
    status, body = _request(f"{base}/api/analyses", fields)
    if status not in (200, 201) or chips["summary"] not in body or chips["sentiment"] not in body:
        return f"API analyze failed (status {status}): summary/sentiment not returned; body: {body[:300]}"

    if len(_chat_calls) < 3:
        return (
            f"only {len(_chat_calls)} chat/completions call(s) observed for 3 analyses — "
            "every analysis must reach the model"
        )

    status, body = _request(f"{base}/api/analyses")
    if status != 200 or any(c["summary"] not in body for c in _CASES):
        return f"API list failed (status {status}): all three analyses must be listed; body: {body[:300]}"

    status, body = _request(f"{base}/api/analyses", {"malformed": True}, form=True)
    if not 400 <= status < 500:
        return f"malformed POST /api/analyses body must get a 4xx, got {status}; body: {body[:300]}"
    return None


def _degraded_check(script: Path, stub_port: int) -> str | None:
    """Launch C: a news API returning non-JSON must yield a legible HTML error, app alive."""
    app_port = _free_port()
    proc = _launch_app(script, app_port, stub_port, news_path="/broken")
    try:
        if not _wait_port(app_port, _START_DEADLINE_SECONDS):
            return f"app did not start for the degraded-news launch; output: {_stop_app(proc)}"
        base = f"http://127.0.0.1:{app_port}"

        status, body = _ui_request(f"{base}/search?q=fusion")
        if not re.search(r"news[\s_-]?api|NEWS_API_URL", body, re.IGNORECASE) or "<" not in body:
            return (
                f"degraded news API: /search (status {status}) must render an HTML error "
                f"naming the news API / NEWS_API_URL, not raw output or silent empties; "
                f"body: {body[:300]}"
            )

        status, body = _ui_request(f"{base}/")
        if status != 200:
            return f"degraded news API wedged the server: home page now {status}"
    finally:
        _stop_app(proc)
    return None


def _persistence_check(script: Path, stub_port: int) -> str | None:
    """Launch D: a fresh process on a fresh port must still serve every stored analysis."""
    app_port = _free_port()
    proc = _launch_app(script, app_port, stub_port)
    try:
        if not _wait_port(app_port, _START_DEADLINE_SECONDS):
            return f"app did not come back for the restart launch; output: {_stop_app(proc)}"
        base = f"http://127.0.0.1:{app_port}"

        status, body = _request(f"{base}/api/analyses")
        if status != 200 or any(c["summary"] not in body for c in _CASES):
            return (
                f"persistence failed (status {status}): analyses lost across restart "
                f"(memory-only storage?); body: {body[:300]}"
            )

        status, body = _ui_request(f"{base}/")
        if status != 200 or _CASES[0]["summary"] not in body:
            return (
                f"post-restart display failed (status {status}): analysis not on the "
                f"home page; body: {body[:300]}"
            )
    finally:
        _stop_app(proc)
    return None


def _check_steps(script: Path, stub_port: int) -> str | None:
    """Run every check group in order; return the first failure reason or `None`."""
    reason = _docs_check(script)
    if reason is None:
        reason = _fail_fast_check(script, stub_port)
    if reason is not None:
        return reason

    app_port = _free_port()
    proc = _launch_app(script, app_port, stub_port)
    try:
        if not _wait_port(app_port, _START_DEADLINE_SECONDS):
            return f"app never listened on PORT={app_port}; output: {_stop_app(proc)}"
        reason = _functional_checks(f"http://127.0.0.1:{app_port}")
        if reason is not None:
            return reason
    finally:
        _stop_app(proc)

    reason = _degraded_check(script, stub_port)
    if reason is not None:
        return reason
    return _persistence_check(script, stub_port)


def main() -> int:
    """Run the probe.

    Returns:
        0 if every check passed against the stub APIs, else 1.
    """
    if len(sys.argv) < 2 or not (Path.cwd() / sys.argv[1]).is_file():
        print(f"probe: entry file not found (expected argv[1]; got {sys.argv[1:]})")
        return 1
    script = Path.cwd() / sys.argv[1]

    stub = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    stub_port = stub.server_address[1]
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    try:
        reason = _check_steps(script, stub_port)
    finally:
        stub.shutdown()
        stub.server_close()

    if reason is None:
        print(
            f"probe: full pipeline ok via {script.name} "
            f"(docs, fail-fast config, 2-query UI search, per-result analyze, "
            f"{len(_chat_calls)} chat call(s), degraded-news error page, "
            f"persisted across restart)"
        )
        return 0
    print(f"probe: {reason}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
