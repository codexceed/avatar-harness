"""Functional success probe for `ecommerce-portal` — ACID under concurrency, end to end.

Usage: ``python shop_portal_smoke.py <entry_file>`` with the scratch repo as cwd. The probe
stands up ONE stub server playing the payment processor (``POST`` anywhere on it): it records
every call (order_id, user_id, product_id, quantity, amount, per-order attempt counts, peak
concurrent in-flight calls), verifies each ``amount`` against the fixture catalog, and enacts
a DETERMINISTIC schedule keyed on the ``user_id`` echoed in the payload — ``retry-*`` users
get a 503 on each order's first attempt, ``doomed-*`` users always 503, ``slow-<i>`` users a
3-9 s hold, everyone else a 0.25 s hold — the hermetic stand-in for "3-15 s, ~5% transient
failure". The app under test is exercised across two launches plus a fail-fast launch:

    A  launched WITHOUT PAYMENT_API_URL  -> exits non-zero fast, naming the variable
    B  UI walk (browser-shaped requests) -> home/search/cart/checkout/orders round-trip on
                                            P-LMP; malformed API bodies -> 4xx
    C  oversell wave                     -> 20 users race checkout on P-ORB (stock 5), half
                                            via UI forms, half via the API: exactly 5 orders,
                                            15 legible denials, stock never negative
    D  multi-line atomicity              -> a cart holding P-PEN + sold-out P-ORB is denied
                                            whole: no partial reservation
    E  retries                           -> first-attempt 503s must be retried to completion;
                                            an always-503 order ends `failed` and restocks
    F  cancel/restock storm              -> 10 cancels race 20 fresh checkouts on P-CBL, then
                                            a sequential drain: conservation ledger holds
    G  cache consistency                 -> a WARMED (X-Cache: hit) query must drop a product
                                            the moment stock hits zero, and resurface it on
                                            cancellation restock
    H  responsiveness under load         -> 30 checkouts with 3-9 s payment holds: every
                                            checkout POST and every browse/search read
                                            answers < 2 s; payment calls overlap (peak >= 2)
    I  exact final ledger + restart      -> inventories, /api/metrics (units_sold, revenue,
                                            orders_by_status) match the precomputed ledger,
                                            and survive a restart on a fresh port

Every assertion is schedule-invariant: aggregates, conservation ledgers, and definitive
per-request outcomes — never "which user won the race". Concurrent waves are followed by
barriers (poll to terminal status) and, where a race leaves a range (the storm), a sequential
drain restores an exact final state. Strict by design: an app that oversells, part-reserves,
completes orders without a payment call, never retries, processes orders one at a time,
serves a stale cached search after a sellout, blocks checkout on the processor, loses state
on restart, or serves a working API with no operable UI fails, with the reason printed.

Exit codes: 0 = every check passed; 1 = a check failed (reason printed).
"""

import contextlib
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
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The probe's independent copy of the fixture catalog: id -> (title, cost, starting stock).
_CATALOG: dict[str, tuple[str, int, int]] = {
    "P-ORB": ("Aurora Orb Lamp", 40, 5),
    "P-TEA": ("Titanium Tea Kettle", 60, 2),
    "P-CBL": ("Braided Cable 2m", 12, 10),
    "P-MUG": ("Thermal Mug", 18, 100),
    "P-PEN": ("Gel Pen Set", 9, 3),
    "P-LMP": ("Anglepoise Desk Lamp", 55, 7),
    "P-COF": ("Pour-Over Coffee Kit", 35, 4),
}
# The exact end-of-run ledger (see the module docstring's phase plan): B sells 1 P-LMP;
# C sells 5 P-ORB; E sells 9 P-MUG (1 doomed order restocks); F turns over the full 10 P-CBL
# (10 completed net, 10 cancelled); G sells 2 P-TEA and cancels 1; H sells 30 P-MUG.
_EXPECTED_INVENTORY = {"P-ORB": 0, "P-TEA": 1, "P-CBL": 0, "P-MUG": 61, "P-PEN": 3, "P-LMP": 6, "P-COF": 4}
_EXPECTED_UNITS_SOLD = 56
_EXPECTED_REVENUE = 1137
_EXPECTED_BY_STATUS = {"completed": 56, "cancelled": 11, "failed": 1}
_TERMINAL = frozenset({"completed", "failed", "cancelled"})

_START_DEADLINE_SECONDS = 20.0
_FAIL_FAST_DEADLINE_SECONDS = 10.0
_SETTLE_DEADLINE_SECONDS = 90.0
_UI_LATENCY_BOUND_SECONDS = 2.0


def _hold_for(index: int) -> float:
    """The payment hold for ``slow-<index>`` users: 9 s once, else 3-5 s (the 3-15 s contract)."""
    return 9.0 if index == 0 else float(3 + index % 3)


class _PaymentLedger:
    """Thread-safe record of every payment call the stub observed."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[dict] = []
        self.attempts: dict[str, int] = {}
        self.payload_errors: list[str] = []
        self.in_flight = 0
        self.peak_in_flight = 0

    def begin(self) -> None:
        """Mark a payment call in flight (tracks the concurrent peak)."""
        with self.lock:
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)

    def end(self) -> None:
        """Mark a payment call finished."""
        with self.lock:
            self.in_flight -= 1

    def record(self, payload: dict) -> int:
        """Validate + store one call's payload; return this order's attempt number."""
        missing = [k for k in ("order_id", "user_id", "product_id", "quantity", "amount") if k not in payload]
        with self.lock:
            if missing:
                self.payload_errors.append(f"payment payload missing {missing}: {payload}")
            order_id = str(payload.get("order_id"))
            self.attempts[order_id] = self.attempts.get(order_id, 0) + 1
            self.calls.append(dict(payload))
            product = _CATALOG.get(str(payload.get("product_id", "")))
            if product is not None:
                try:
                    expected = product[1] * float(payload.get("quantity", 0))
                    if abs(float(payload.get("amount", -1)) - expected) > 0.01:
                        self.payload_errors.append(
                            f"amount {payload.get('amount')} != cost*quantity {expected} for {payload}"
                        )
                except (TypeError, ValueError):
                    self.payload_errors.append(f"non-numeric quantity/amount: {payload}")
            return self.attempts[order_id]

    def reset_peak(self) -> None:
        """Zero the concurrency peak (called before the load phase measures overlap)."""
        with self.lock:
            self.peak_in_flight = self.in_flight

    def calls_for(self, product_id: str) -> list[dict]:
        """The recorded calls for one product."""
        with self.lock:
            return [c for c in self.calls if str(c.get("product_id")) == product_id]

    def attempts_for(self, order_id: str) -> int:
        """How many attempts the stub saw for one order id."""
        with self.lock:
            return self.attempts.get(str(order_id), 0)


_LEDGER = _PaymentLedger()


class _StubHandler(BaseHTTPRequestHandler):
    """The payment processor: slow, flaky on a deterministic per-user schedule."""

    def log_message(self, format: str, *args: object) -> None:
        """Silence per-request logging (stdlib override signature)."""

    def do_POST(self) -> None:
        """Accept a payment request, enact the schedule, reply ok / transient_error."""
        _LEDGER.begin()
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            try:
                payload = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                _LEDGER.payload_errors.append(f"non-JSON payment body: {raw[:120]!r}")
                self._reply(400, {"status": "bad_request"})
                return
            attempt = _LEDGER.record(payload)
            user = str(payload.get("user_id", ""))
            if user.startswith("doomed-"):
                self._reply(503, {"status": "transient_error"})
                return
            if user.startswith("retry-") and attempt == 1:
                self._reply(503, {"status": "transient_error"})
                return
            if user.startswith("slow-"):
                with contextlib.suppress(ValueError):
                    time.sleep(_hold_for(int(user.split("-", 1)[1])))
            else:
                time.sleep(0.25)
            self._reply(200, {"status": "ok", "transaction_id": f"txn-{payload.get('order_id')}"})
        finally:
            _LEDGER.end()

    def _reply(self, code: int, payload: dict) -> None:
        # A dying/timing-out app may drop the connection mid-reply; that is the app's
        # problem (its checks will fail), not a stub crash worth a stderr traceback.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


# --------------------------------------------------------------------------- #
# HTTP + process helpers
# --------------------------------------------------------------------------- #
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


def _request(
    url: str,
    payload: dict | None = None,
    *,
    form: bool = False,
    timeout: float = 15.0,
) -> tuple[int, str, dict[str, str]]:
    """GET `url` (or POST `payload`), returning (status, body, lowercased headers), no raise.

    `form=True` posts `payload` form-encoded — the same request a browser form submit sends —
    instead of JSON; redirects are followed either way.
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
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — localhost stub/app
            return (
                resp.status,
                resp.read().decode("utf-8", errors="replace"),
                {k.lower(): v for k, v in resp.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        return (
            exc.code,
            exc.read().decode("utf-8", errors="replace"),
            {k.lower(): v for k, v in (exc.headers or {}).items()},
        )
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return 0, f"(request failed: {exc})", {}


def _ui_request(url: str, payload: dict | None = None, *, form: bool = False) -> tuple[int, str]:
    """`_request` with the body HTML-unescaped, for content checks against served pages."""
    import html

    status, body, _ = _request(url, payload, form=form)
    return status, html.unescape(body)


def _has_form(page: str, action: str, input_name: str | None = None) -> bool:
    """Whether `page` has a `<form>` targeting `action` (quoting/attribute-order agnostic)."""
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


def _launch_app(script: Path, port: int, stub_port: int, *, omit: str | None = None) -> subprocess.Popen[str]:
    """Start the app under test with the payment stub wired in via env."""
    env = {k: v for k, v in os.environ.items() if k != "PAYMENT_API_URL"}
    env.update({"PORT": str(port), "PAYMENT_API_URL": f"http://127.0.0.1:{stub_port}/pay"})
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
# Portal-flow helpers (the API is the probe's measurement surface)
# --------------------------------------------------------------------------- #
def _inventory(base: str, product_id: str) -> int | None:
    """Current stock of one product via ``/api/products/<id>``, or `None` on any failure."""
    status, body, _ = _request(f"{base}/api/products/{product_id}")
    if status != 200:
        return None
    try:
        value = json.loads(body).get("inventory")
        return int(value) if value is not None else None
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return None


def _orders(base: str, user: str) -> list[dict] | None:
    """The user's orders via ``/api/orders`` — a JSON array per the contract, else `None`."""
    status, body, _ = _request(f"{base}/api/orders?user_id={urllib.parse.quote(user)}")
    if status != 200:
        return None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def _cart_add(base: str, user: str, product_id: str, *, via_ui: bool = False) -> tuple[int, str]:
    """Add one unit to the user's cart through either surface."""
    if via_ui:
        return _ui_request(f"{base}/cart/add", {"user_id": user, "product_id": product_id}, form=True)
    status, body, _ = _request(f"{base}/api/cart/add", {"user_id": user, "product_id": product_id})
    return status, body


def _checkout(base: str, user: str, *, via_ui: bool = False) -> tuple[int, str]:
    """Check out the user's cart through either surface."""
    if via_ui:
        return _ui_request(f"{base}/checkout", {"user_id": user}, form=True)
    status, body, _ = _request(f"{base}/api/checkout", {"user_id": user})
    return status, body


def _await_terminal(base: str, users: list[str], deadline_seconds: float) -> str | None:
    """Poll until every user's every order reaches a terminal status; return a failure reason.

    This is a *barrier*, not a latency assertion (those live in `_responsiveness_check`): it
    must be robust to the connection pressure it creates. A single poll that fails to connect
    is retried on the next round rather than aborting — so a transient timeout during a
    30-order settle burst is not mistaken for a stalled pipeline. Only two things fail the
    barrier: orders that never reach a terminal status by the deadline (a genuine stall /
    sequential worker), or a poll that keeps returning a broken (non-array) body to the end.
    A small inter-request pause keeps the poll from saturating the app's accept queue.
    """
    deadline = time.monotonic() + deadline_seconds
    pending: set[str] = set(users)
    last_error: dict[str, str] = {}
    while pending and time.monotonic() < deadline:
        for user in list(pending):
            rows = _orders(base, user)
            if rows is None:
                last_error[user] = "did not return a JSON array of orders"
            elif all(str(r.get("status")) in _TERMINAL for r in rows):
                pending.discard(user)
                last_error.pop(user, None)
            time.sleep(0.01)  # a burst of zero-delay connections saturates a plain HTTP server
        if pending:
            time.sleep(0.25)
    if pending:
        stuck = sorted(pending)[:5]
        broken = [u for u in stuck if u in last_error]
        if broken:
            return f"GET /api/orders?user_id={broken[0]} {last_error[broken[0]]}"
        return (
            f"orders for {stuck} never reached a terminal status within "
            f"{deadline_seconds:.0f}s — background processing looks stalled or sequential"
        )
    return None


class _StockMonitor:
    """Samples one product's stock during a wave; only a NEGATIVE reading is a violation."""

    def __init__(self, base: str, product_id: str) -> None:
        self._base = base
        self._product_id = product_id
        self._stop = threading.Event()
        self.negative_seen: int | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            value = _inventory(self._base, self._product_id)
            if value is not None and value < 0:
                self.negative_seen = value
                return
            time.sleep(0.03)

    def __enter__(self) -> "_StockMonitor":
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


# --------------------------------------------------------------------------- #
# The checks, phase by phase
# --------------------------------------------------------------------------- #
def _fail_fast_check(script: Path, stub_port: int) -> str | None:
    """Phase A: missing PAYMENT_API_URL must exit non-zero fast, naming the variable."""
    proc = _launch_app(script, _free_port(), stub_port, omit="PAYMENT_API_URL")
    try:
        out, err = proc.communicate(timeout=_FAIL_FAST_DEADLINE_SECONDS)
    except subprocess.TimeoutExpired:
        _stop_app(proc)
        return "launched without PAYMENT_API_URL the app kept running — it must exit non-zero at startup"
    if proc.returncode == 0:
        return "launched without PAYMENT_API_URL the app exited 0 — required config must be an error"
    if "PAYMENT_API_URL" not in (out or "") + (err or ""):
        combined = ((out or "") + (err or "")).strip()[:300]
        return f"the startup error does not name PAYMENT_API_URL; output: {combined}"
    return None


def _ui_walk(base: str) -> str | None:  # noqa: C901, PLR0911, PLR0912 — a flat step gauntlet; each failure returns its reason
    """Phase B: one honest user round-trips home -> search -> cart -> checkout -> orders."""
    orb_title, lamp_title = _CATALOG["P-ORB"][0], _CATALOG["P-LMP"][0]

    status, body = _ui_request(f"{base}/")
    if status != 200 or not _has_form(body, "/search", input_name="q"):
        return f"home page failed (status {status}): no search form with input q; body: {body[:300]}"
    if orb_title not in body or _CATALOG["P-MUG"][0] not in body:
        return f"home page must list in-stock products (titles); body: {body[:300]}"

    status, body = _ui_request(f"{base}/search?q=lamp")
    if status != 200 or orb_title not in body or lamp_title not in body:
        return f"/search?q=lamp (status {status}) must render both lamp products; body: {body[:300]}"
    if not _has_form(body, "/cart/add", input_name="user_id"):
        return "search results must offer an add-to-cart form (POST /cart/add with a user_id input)"

    status, body = _cart_add(base, "walk-1", "P-LMP", via_ui=True)
    if status not in (200, 201):
        return f"UI add-to-cart failed (status {status}); body: {body[:300]}"

    status, body = _ui_request(f"{base}/cart?user_id=walk-1")
    if status != 200 or lamp_title not in body or not _has_form(body, "/checkout", input_name="user_id"):
        return f"cart page (status {status}) must show the line + a checkout form; body: {body[:300]}"

    started = time.monotonic()
    status, body = _checkout(base, "walk-1", via_ui=True)
    if status not in (200, 201):
        return f"UI checkout failed (status {status}); body: {body[:300]}"
    if time.monotonic() - started > _UI_LATENCY_BOUND_SECONDS:
        return "UI checkout blocked past 2 s — processing must happen in the background"

    reason = _await_terminal(base, ["walk-1"], _SETTLE_DEADLINE_SECONDS)
    if reason is not None:
        return reason
    rows = _orders(base, "walk-1") or []
    if len(rows) != 1 or str(rows[0].get("status")) != "completed":
        return f"the walk order must end completed; orders: {rows}"
    if len(_LEDGER.calls_for("P-LMP")) != 1:
        return f"expected exactly 1 payment call for the P-LMP order, saw {len(_LEDGER.calls_for('P-LMP'))}"

    status, body = _ui_request(f"{base}/orders?user_id=walk-1")
    if status != 200 or lamp_title not in body or "completed" not in body:
        return f"orders page (status {status}) must show the completed order; body: {body[:300]}"
    if not _has_form(body, "/orders/cancel", input_name="order_id"):
        return (
            "orders page must offer a cancel form (POST /orders/cancel with order_id) for a completed order"
        )

    status, body, _ = _request(f"{base}/api/checkout", {"malformed": True})
    if not 400 <= status < 500:
        return f"malformed POST /api/checkout must get a 4xx, got {status}; body: {body[:300]}"
    return None


def _oversell_wave(base: str) -> str | None:  # noqa: C901, PLR0911, PLR0912 — a flat step gauntlet
    """Phase C: 20 users race checkout on 5 units, mixed surfaces: exactly 5 win, 0 oversell."""
    users = [f"orb-{i}" for i in range(20)]
    for i, user in enumerate(users):
        status, body = _cart_add(base, user, "P-ORB", via_ui=i % 2 == 0)
        if status not in (200, 201):
            return f"cart add for {user} failed (status {status}) — carting must not depend on stock"

    with _StockMonitor(base, "P-ORB") as monitor, ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda iu: _checkout(base, iu[1], via_ui=iu[0] % 2 == 0), enumerate(users)))
    if monitor.negative_seen is not None:
        return f"P-ORB inventory went NEGATIVE ({monitor.negative_seen}) during the checkout wave"

    winners: list[str] = []
    total_orders = 0
    for user, (status, body) in zip(users, results, strict=True):
        rows = _orders(base, user)
        if rows is None:
            return f"GET /api/orders?user_id={user} did not return a JSON array after the wave"
        total_orders += len(rows)
        if rows:
            winners.append(user)
        else:
            named = "P-ORB" in body or _CATALOG["P-ORB"][0] in body
            if not named or (status != 409 and not 200 <= status < 300):
                return (
                    f"denied checkout for {user} must be a legible denial naming P-ORB "
                    f"(status {status}); body: {body[:300]}"
                )
    if len(winners) != 5 or total_orders != 5:
        return (
            f"stock 5 + 20 racing checkouts must yield exactly 5 orders; "
            f"got {total_orders} across {len(winners)} winners"
        )

    reason = _await_terminal(base, winners, _SETTLE_DEADLINE_SECONDS)
    if reason is not None:
        return reason
    for user in winners:
        rows = _orders(base, user) or []
        if len(rows) != 1 or str(rows[0].get("status")) != "completed":
            return f"winner {user}'s order must end completed; orders: {rows}"
    if _inventory(base, "P-ORB") != 0:
        return f"P-ORB inventory must be exactly 0 after the wave, got {_inventory(base, 'P-ORB')}"
    if len(_LEDGER.calls_for("P-ORB")) != 5:
        return (
            f"expected exactly 5 P-ORB payment calls (one per reserved order), "
            f"saw {len(_LEDGER.calls_for('P-ORB'))} — payment must never run for denied checkouts"
        )
    return None


def _atomicity_check(base: str) -> str | None:
    """Phase D: a cart holding P-PEN + sold-out P-ORB must be denied WHOLE."""
    for product in ("P-PEN", "P-ORB"):
        status, _ = _cart_add(base, "atom-1", product)
        if status not in (200, 201):
            return f"cart add of {product} for atom-1 failed (status {status})"
    status, body = _checkout(base, "atom-1")
    if status != 409 or ("P-ORB" not in body and _CATALOG["P-ORB"][0] not in body):
        return f"mixed cart with a sold-out line must 409 naming P-ORB; got {status}: {body[:300]}"
    if _orders(base, "atom-1"):
        return "a denied checkout must create no orders (partial reservation detected)"
    if _inventory(base, "P-PEN") != 3:
        return (
            f"P-PEN stock must be untouched (3) after the denied mixed cart, got {_inventory(base, 'P-PEN')}"
        )
    if _LEDGER.calls_for("P-PEN"):
        return "no payment call may be made for a denied checkout's lines"
    return None


def _retry_check(base: str) -> str | None:  # noqa: C901, PLR0911, PLR0912 — a flat step gauntlet
    """Phase E: transient 503s are retried to completion; a doomed order fails + restocks."""
    users = [f"retry-{i}" for i in range(1, 4)] + ["doomed-1"] + [f"bulk-{i}" for i in range(1, 7)]
    for user in users:
        status, _ = _cart_add(base, user, "P-MUG")
        if status not in (200, 201):
            return f"cart add for {user} failed (status {status})"
    with ThreadPoolExecutor(max_workers=10) as pool:
        checkouts = list(pool.map(lambda u: _checkout(base, u), users))
    for user, (status, body) in zip(users, checkouts, strict=True):
        if not 200 <= status < 300:
            return f"checkout for {user} failed (status {status}); body: {body[:300]}"

    reason = _await_terminal(base, users, _SETTLE_DEADLINE_SECONDS)
    if reason is not None:
        return reason
    for user in users:
        rows = _orders(base, user) or []
        if len(rows) != 1:
            return f"{user} must have exactly one order, got {len(rows)}"
        order_id, status_value = str(rows[0].get("id")), str(rows[0].get("status"))
        attempts = _LEDGER.attempts_for(order_id)
        if user == "doomed-1":
            if status_value != "failed":
                return f"the always-503 order must end failed, got {status_value!r}"
            if not 2 <= attempts <= 5:
                return f"the doomed order needs retries within the budget (2..5 attempts), saw {attempts}"
        elif status_value != "completed":
            return f"{user}'s order must end completed (transient 503s are retryable), got {status_value!r}"
        elif user.startswith("retry-") and attempts < 2:
            return f"{user}'s order completed with {attempts} attempt(s) — the 503 was never retried"
        elif user.startswith("bulk-") and attempts != 1:
            return f"{user}'s clean order should need exactly 1 attempt, saw {attempts}"
    if _inventory(base, "P-MUG") != 91:
        return (
            f"P-MUG must be 91 after 10 checkouts with 1 failed-and-restocked order, "
            f"got {_inventory(base, 'P-MUG')}"
        )
    return None


def _cancel_restock_storm(base: str) -> str | None:  # noqa: C901, PLR0911, PLR0912, PLR0915 — a flat step gauntlet
    """Phase F: 10 cancels race 20 fresh checkouts; conservation holds; a drain exacts the ledger."""
    first_wave = [f"cbl-{i}" for i in range(1, 11)]
    for user in first_wave:
        status, _ = _cart_add(base, user, "P-CBL")
        if status not in (200, 201):
            return f"cart add for {user} failed (status {status})"
    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda u: _checkout(base, u), first_wave))
    reason = _await_terminal(base, first_wave, _SETTLE_DEADLINE_SECONDS)
    if reason is not None:
        return reason
    order_by_user: dict[str, str] = {}
    for user in first_wave:
        rows = _orders(base, user) or []
        if len(rows) != 1 or str(rows[0].get("status")) != "completed":
            return f"first-wave {user} must hold one completed P-CBL order; got {rows}"
        order_by_user[user] = str(rows[0].get("id"))
    if _inventory(base, "P-CBL") != 0:
        return f"P-CBL must be sold out (0) before the storm, got {_inventory(base, 'P-CBL')}"

    second_wave = [f"cblw-{i}" for i in range(1, 21)]
    for i, user in enumerate(second_wave):
        status, _ = _cart_add(base, user, "P-CBL", via_ui=i % 2 == 0)
        if status not in (200, 201):
            return f"cart add for {user} failed (status {status})"

    def _cancel(user: str) -> tuple[int, str]:
        order_id = order_by_user[user]
        if int(user.rsplit("-", 1)[1]) % 2 == 0:
            return _ui_request(f"{base}/orders/cancel", {"user_id": user, "order_id": order_id}, form=True)
        status, body, _ = _request(f"{base}/api/orders/{order_id}/cancel", {"user_id": user})
        return status, body

    tasks = [lambda u=u: _cancel(u) for u in first_wave]
    tasks += [lambda iu=iu: _checkout(base, iu[1], via_ui=iu[0] % 2 == 0) for iu in enumerate(second_wave)]
    with _StockMonitor(base, "P-CBL") as monitor, ThreadPoolExecutor(max_workers=30) as pool:
        results = list(pool.map(lambda fn: fn(), tasks))
    if monitor.negative_seen is not None:
        return f"P-CBL inventory went NEGATIVE ({monitor.negative_seen}) during the cancel/checkout storm"
    for user, (status, body) in zip(first_wave, results[:10], strict=True):
        if not 200 <= status < 300:
            return f"cancelling {user}'s completed order failed (status {status}); body: {body[:300]}"
        rows = _orders(base, user) or []
        if not rows or str(rows[0].get("status")) != "cancelled":
            return f"{user}'s order must be cancelled after the storm; got {rows}"

    settled_users = list(second_wave)
    reason = _await_terminal(base, settled_users, _SETTLE_DEADLINE_SECONDS)
    if reason is not None:
        return reason
    completed_net = 0
    for user, (status, body) in zip(second_wave, results[10:], strict=True):
        rows = _orders(base, user)
        if rows is None:
            return f"GET /api/orders?user_id={user} did not return a JSON array after the storm"
        if rows:
            if len(rows) != 1 or str(rows[0].get("status")) != "completed":
                return f"storm winner {user} must hold one completed order; got {rows}"
            completed_net += 1
        elif "P-CBL" not in body and _CATALOG["P-CBL"][0] not in body:
            return f"storm loser {user} got no legible P-CBL denial (status {status}); body: {body[:300]}"
    stock = _inventory(base, "P-CBL")
    if stock is None or stock < 0 or completed_net + stock != 10:
        return (
            f"conservation violated after the storm: {completed_net} completed (net) + "
            f"{stock} in stock != the 10 units cancellation returned"
        )

    drained = 0
    for _ in range(12):
        status, _ = _cart_add(base, "drain-1", "P-CBL")
        if status not in (200, 201):
            return f"drain cart add failed (status {status})"
        status, body = _checkout(base, "drain-1")
        if status == 409:
            if _inventory(base, "P-CBL") != 0:
                return f"drain denied while stock is {_inventory(base, 'P-CBL')} — denial must mean sold out"
            break
        if not 200 <= status < 300:
            return f"drain checkout failed (status {status}); body: {body[:300]}"
        reason = _await_terminal(base, ["drain-1"], _SETTLE_DEADLINE_SECONDS)
        if reason is not None:
            return reason
        drained += 1
    else:
        return "the drain never hit a sold-out denial — restocked stock appears unbounded"
    if drained != 10 - completed_net or _inventory(base, "P-CBL") != 0:
        return (
            f"the drain sold {drained} units; with {completed_net} storm sales it must total the "
            f"10 restocked units and leave 0 in stock (got {_inventory(base, 'P-CBL')})"
        )
    return None


def _cache_consistency(base: str) -> str | None:  # noqa: C901, PLR0911, PLR0912 — a flat step gauntlet
    """Phase G: a warmed query drops a sold-out product immediately and restores it on restock."""
    kettle = _CATALOG["P-TEA"][0]
    hit_seen = False
    for _ in range(10):
        status, body, headers = _request(f"{base}/api/search?q=tea")
        if status != 200 or kettle not in body:
            return (
                f"/api/search?q=tea (status {status}) must list the kettle while in stock; body: {body[:300]}"
            )
        cache_state = headers.get("x-cache", "")
        if cache_state not in ("hit", "miss"):
            return f"/api/search must send X-Cache: hit|miss on every response, got {cache_state!r}"
        if cache_state == "hit":
            hit_seen = True
            break
    if not hit_seen:
        return "10 identical searches (no inventory change between) never produced X-Cache: hit"

    buyers = ["tea-1", "tea-2"]
    for user in buyers:
        status, _ = _cart_add(base, user, "P-TEA")
        if status not in (200, 201):
            return f"cart add for {user} failed (status {status})"
        status, body = _checkout(base, user)
        if not 200 <= status < 300:
            return f"checkout for {user} failed (status {status}); body: {body[:300]}"
    reason = _await_terminal(base, buyers, _SETTLE_DEADLINE_SECONDS)
    if reason is not None:
        return reason
    if _inventory(base, "P-TEA") != 0:
        return f"P-TEA must be sold out after both buys, got {_inventory(base, 'P-TEA')}"

    status, body, headers = _request(f"{base}/api/search?q=tea")
    if status != 200 or kettle in body:
        return (
            f"sold-out product still surfaced by /api/search?q=tea (status {status}, "
            f"X-Cache {headers.get('x-cache')!r}) — the cache must never outlive a sellout"
        )
    status, body = _ui_request(f"{base}/search?q=tea")
    if status != 200 or kettle in body:
        return f"sold-out product still surfaced by the UI /search?q=tea (status {status})"
    status, body = _ui_request(f"{base}/search?q=lamp")
    if status != 200 or _CATALOG["P-ORB"][0] in body or _CATALOG["P-LMP"][0] not in body:
        return f"/search?q=lamp must drop sold-out P-ORB and keep P-LMP (status {status}); body: {body[:300]}"

    rows = _orders(base, "tea-1") or []
    if len(rows) != 1:
        return f"tea-1 must hold one order, got {rows}"
    status, body, _ = _request(f"{base}/api/orders/{rows[0].get('id')}/cancel", {"user_id": "tea-1"})
    if not 200 <= status < 300:
        return f"cancelling tea-1's completed order failed (status {status}); body: {body[:300]}"
    if _inventory(base, "P-TEA") != 1:
        return f"P-TEA must restock to 1 after the cancellation, got {_inventory(base, 'P-TEA')}"
    status, body, _ = _request(f"{base}/api/search?q=tea")
    if status != 200 or kettle not in body:
        return f"restocked product must resurface in /api/search?q=tea (status {status}); body: {body[:300]}"
    status, body = _ui_request(f"{base}/search?q=tea")
    if status != 200 or kettle not in body:
        return f"restocked product must resurface in the UI /search?q=tea (status {status})"
    return None


def _responsiveness_check(base: str) -> str | None:  # noqa: C901, PLR0911, PLR0912 — a flat step gauntlet
    """Phase H: 30 slow-payment checkouts; every write and read answers < 2 s; calls overlap."""
    users = [f"slow-{i}" for i in range(30)]
    for user in users:
        status, _ = _cart_add(base, user, "P-MUG")
        if status not in (200, 201):
            return f"cart add for {user} failed (status {status})"

    _LEDGER.reset_peak()

    def _timed_checkout(user: str) -> tuple[float, int, str]:
        started = time.monotonic()
        status, body = _checkout(base, user)
        return time.monotonic() - started, status, body

    with ThreadPoolExecutor(max_workers=30) as pool:
        futures = [pool.submit(_timed_checkout, user) for user in users]
        time.sleep(1.0)  # payment holds are 3-9 s: the pipeline is now saturated
        for i in range(10):
            path = "/" if i % 2 == 0 else "/search?q=mug"
            started = time.monotonic()
            status, body = _ui_request(f"{base}{path}")
            elapsed = time.monotonic() - started
            if status != 200:
                return f"GET {path} failed (status {status}) while orders were processing; body: {body[:300]}"
            if elapsed > _UI_LATENCY_BOUND_SECONDS:
                return f"GET {path} took {elapsed:.1f}s under order load — browsing must stay responsive"
            time.sleep(0.4)
        checkout_results = [f.result() for f in futures]

    for user, (elapsed, status, body) in zip(users, checkout_results, strict=True):
        if not 200 <= status < 300:
            return f"checkout for {user} failed (status {status}); body: {body[:300]}"
        if elapsed > _UI_LATENCY_BOUND_SECONDS:
            return f"checkout for {user} took {elapsed:.1f}s — it must answer < 2 s and process in background"

    reason = _await_terminal(base, users, _SETTLE_DEADLINE_SECONDS)
    if reason is not None:
        return reason
    if _LEDGER.peak_in_flight < 2:
        return "payment calls never overlapped during the load phase — orders must be processed concurrently"
    for user in users:
        rows = _orders(base, user) or []
        if len(rows) != 1 or str(rows[0].get("status")) != "completed":
            return f"load-phase {user} must end with one completed order; got {rows}"
    if _inventory(base, "P-MUG") != 61:
        return f"P-MUG must be 61 after the load phase, got {_inventory(base, 'P-MUG')}"
    return None


def _final_ledger(base: str, *, phase: str) -> str | None:  # noqa: C901, PLR0911 — a flat step gauntlet
    """Phase I: the exact end-of-run inventories and sales metrics (also re-run post-restart)."""
    for product_id, expected in _EXPECTED_INVENTORY.items():
        actual = _inventory(base, product_id)
        if actual != expected:
            return f"[{phase}] {product_id} inventory must be {expected}, got {actual}"
    status, body, _ = _request(f"{base}/api/metrics")
    if status != 200:
        return f"[{phase}] GET /api/metrics failed (status {status}); body: {body[:300]}"
    try:
        metrics = json.loads(body)
    except json.JSONDecodeError:
        return f"[{phase}] /api/metrics must return JSON; body: {body[:300]}"
    if int(metrics.get("units_sold", -1)) != _EXPECTED_UNITS_SOLD:
        return f"[{phase}] units_sold must be {_EXPECTED_UNITS_SOLD}, got {metrics.get('units_sold')}"
    if abs(float(metrics.get("revenue", -1)) - _EXPECTED_REVENUE) > 0.01:
        return f"[{phase}] revenue must be {_EXPECTED_REVENUE}, got {metrics.get('revenue')}"
    by_status = metrics.get("orders_by_status") or {}
    for status_name, expected_count in _EXPECTED_BY_STATUS.items():
        if int(by_status.get(status_name, 0)) != expected_count:
            return f"[{phase}] orders_by_status[{status_name}] must be {expected_count}, got {by_status}"
    for live in ("pending", "processing"):
        if int(by_status.get(live, 0)) != 0:
            return f"[{phase}] no order may be left {live} at rest; got {by_status}"
    return None


def _restart_check(script: Path, stub_port: int) -> str | None:
    """Phase I (b): a fresh process on a fresh port must serve the same ledger (real durability)."""
    app_port = _free_port()
    proc = _launch_app(script, app_port, stub_port)
    try:
        if not _wait_port(app_port, _START_DEADLINE_SECONDS):
            return f"app did not come back for the restart launch; output: {_stop_app(proc)}"
        base = f"http://127.0.0.1:{app_port}"
        reason = _final_ledger(base, phase="post-restart")
        if reason is not None:
            return reason + " (state lost across restart, or the catalog was reseeded)"
        status, body = _ui_request(f"{base}/orders?user_id=walk-1")
        if status != 200 or _CATALOG["P-LMP"][0] not in body or "completed" not in body:
            return f"post-restart orders page lost the walk order (status {status}); body: {body[:300]}"
        status, body = _ui_request(f"{base}/")
        if status != 200 or _CATALOG["P-ORB"][0] in body or _CATALOG["P-MUG"][0] not in body:
            return f"post-restart home page must keep listing in-stock only (status {status})"
    finally:
        _stop_app(proc)
    return None


def _check_steps(script: Path, stub_port: int) -> str | None:
    """Run every phase in order; return the first failure reason or `None`."""
    reason = _fail_fast_check(script, stub_port)
    if reason is not None:
        return reason

    app_port = _free_port()
    proc = _launch_app(script, app_port, stub_port)
    try:
        if not _wait_port(app_port, _START_DEADLINE_SECONDS):
            return f"app never listened on PORT={app_port}; output: {_stop_app(proc)}"
        base = f"http://127.0.0.1:{app_port}"
        for check in (
            _ui_walk,
            _oversell_wave,
            _atomicity_check,
            _retry_check,
            _cancel_restock_storm,
            _cache_consistency,
            _responsiveness_check,
        ):
            reason = check(base)
            if reason is not None:
                return reason
        if _LEDGER.payload_errors:
            return f"payment payload violations: {_LEDGER.payload_errors[0]}"
        reason = _final_ledger(base, phase="final")
        if reason is not None:
            return reason
    finally:
        _stop_app(proc)

    return _restart_check(script, stub_port)


def main() -> int:
    """Run the probe.

    Returns:
        0 if every check passed against the payment stub, else 1.
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
            f"probe: full portal ok via {script.name} "
            f"(fail-fast config, UI walk, 20-way oversell wave held at 5, atomic mixed cart, "
            f"retries + failed-order restock, cancel/restock storm + drain, warm-cache sellout "
            f"consistency, {_LEDGER.peak_in_flight} peak concurrent payments under load, "
            f"exact ledger persisted across restart)"
        )
        return 0
    print(f"probe: {reason}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
