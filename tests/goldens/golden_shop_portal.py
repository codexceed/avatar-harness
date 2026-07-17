
"""Golden reference portal for probe validation — stdlib only, per the task contract.

Configuration: PORT (optional, default 8000); PAYMENT_API_URL (required, fail-fast).
"""

import html
import json
import os
import queue
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DB_PATH = "shop.db"
PAYMENT_API_URL = os.environ.get("PAYMENT_API_URL", "")

ORDER_QUEUE: "queue.Queue[int]" = queue.Queue()
_VERSION_LOCK = threading.Lock()
_INVENTORY_VERSION = 0
_CACHE: dict[tuple[str, int], list[dict]] = {}
_CACHE_LOCK = threading.Lock()


def bump_version() -> None:
    global _INVENTORY_VERSION
    with _VERSION_LOCK:
        _INVENTORY_VERSION += 1


def current_version() -> int:
    with _VERSION_LOCK:
        return _INVENTORY_VERSION


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS products(
            id TEXT PRIMARY KEY, title TEXT, description TEXT, cost REAL, inventory INTEGER);
        CREATE TABLE IF NOT EXISTS carts(user_id TEXT, product_id TEXT, quantity INTEGER);
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, product_id TEXT,
            quantity INTEGER, amount REAL, status TEXT);
        """
    )
    if conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"] == 0:
        rows = json.loads(Path("products.json").read_text(encoding="utf-8"))
        conn.executemany(
            "INSERT INTO products VALUES(?,?,?,?,?)",
            [(r["id"], r["title"], r["description"], r["cost"], r["inventory"]) for r in rows],
        )
    conn.commit()
    # Recover orders that were mid-pipeline when the last process stopped.
    live = "SELECT id FROM orders WHERE status IN ('pending','processing')"
    stranded = [r["id"] for r in conn.execute(live)]
    conn.close()
    for order_id in stranded:
        ORDER_QUEUE.put(order_id)


def process_order(order_id: int) -> None:
    conn = connect()
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if row is None or row["status"] not in ("pending", "processing"):
        conn.close()
        return
    conn.execute("UPDATE orders SET status='processing' WHERE id=?", (order_id,))
    conn.commit()
    payload = json.dumps(
        {
            "order_id": order_id,
            "user_id": row["user_id"],
            "product_id": row["product_id"],
            "quantity": row["quantity"],
            "amount": row["amount"],
        }
    ).encode("utf-8")
    ok = False
    for attempt in range(5):  # RETRY-BUDGET: up to 5 attempts before the order fails
        try:
            req = urllib.request.Request(
                PAYMENT_API_URL, data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                ok = resp.status == 200
        except urllib.error.HTTPError:
            ok = False  # transient 503 from the processor
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            ok = False
        if ok:
            break
        time.sleep(0.2)
    if ok:
        conn.execute("UPDATE orders SET status='completed' WHERE id=?", (order_id,))
        conn.commit()
    else:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE orders SET status='failed' WHERE id=?", (order_id,))
        conn.execute(
            "UPDATE products SET inventory = inventory + ? WHERE id=?",
            (row["quantity"], row["product_id"]),
        )
        conn.commit()
        bump_version()
    conn.close()


def worker_loop() -> None:
    while True:
        order_id = ORDER_QUEUE.get()
        try:
            process_order(order_id)
        except Exception:
            pass


def search_products(q: str) -> tuple[list[dict], bool]:
    """In-stock substring matches for q; returns (rows, was_cache_hit)."""
    key = (q.strip().lower(), current_version())  # CACHE-KEY: stock version scopes every entry
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key], True
    conn = connect()
    like = f"%{key[0]}%"
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM products WHERE inventory > 0 AND "
            "(LOWER(title) LIKE ? OR LOWER(description) LIKE ?)",
            (like, like),
        )
    ]
    conn.close()
    with _CACHE_LOCK:
        _CACHE[key] = rows
    return rows, False


def checkout(user_id: str) -> tuple[bool, list[int] | dict]:
    """Atomically reserve the whole cart; (True, order_ids) or (False, offending product)."""
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")  # ATOMIC-RESERVE: one write txn covers check + decrement
    lines = conn.execute(
        "SELECT product_id, SUM(quantity) AS q FROM carts WHERE user_id=? GROUP BY product_id",
        (user_id,),
    ).fetchall()
    if not lines:
        conn.rollback()
        conn.close()
        return False, {"error": "cart is empty"}
    for line in lines:
        product = conn.execute("SELECT * FROM products WHERE id=?", (line["product_id"],)).fetchone()
        if product is None or product["inventory"] < line["q"]:
            conn.rollback()
            conn.close()
            return False, dict(product) if product else {"id": line["product_id"], "title": "unknown"}
    order_ids: list[int] = []
    for line in lines:
        product = conn.execute("SELECT * FROM products WHERE id=?", (line["product_id"],)).fetchone()
        conn.execute(
            "UPDATE products SET inventory = inventory - ? WHERE id=?", (line["q"], line["product_id"])
        )
        cur = conn.execute(
            "INSERT INTO orders(user_id, product_id, quantity, amount, status) VALUES(?,?,?,?,'pending')",
            (user_id, line["product_id"], line["q"], product["cost"] * line["q"]),
        )
        order_ids.append(cur.lastrowid)
    conn.execute("DELETE FROM carts WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    bump_version()
    for order_id in order_ids:
        ORDER_QUEUE.put(order_id)  # ASYNC-HANDOFF: workers process; the request returns now
    return True, order_ids


def cancel_order(user_id: str, order_id: int) -> tuple[int, dict]:
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, user_id)
    ).fetchone()
    if row is None:
        conn.rollback()
        conn.close()
        return 404, {"error": "no such order"}
    if row["status"] != "completed":
        conn.rollback()
        conn.close()
        return 409, {"error": f"only completed orders can be cancelled (status: {row['status']})"}
    conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order_id,))
    conn.execute(
        "UPDATE products SET inventory = inventory + ? WHERE id=?", (row["quantity"], row["product_id"])
    )
    conn.commit()
    conn.close()
    bump_version()
    return 200, {"ok": True, "order_id": order_id}


def page(title: str, body: str) -> str:
    return f"<!doctype html><html><head><title>{html.escape(title)}</title></head><body>{body}</body></html>"


def product_forms(rows: list[dict]) -> str:
    parts = []
    for r in rows:
        parts.append(
            f"<li>{html.escape(r['title'])} — ${r['cost']} ({r['inventory']} in stock)"
            f"<form action='/cart/add' method='post'>"
            f"<input name='user_id' placeholder='user id'>"
            f"<input type='hidden' name='product_id' value='{r['id']}'>"
            f"<button type='submit'>Add to cart</button></form></li>"
        )
    return "<ul>" + "".join(parts) + "</ul>"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    # ---- plumbing -------------------------------------------------------------
    def _send(self, code: int, body: str, ctype: str, extra: dict | None = None) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _html(self, code: int, body: str) -> None:
        self._send(code, body, "text/html; charset=utf-8")

    def _json(self, code: int, payload: object, extra: dict | None = None) -> None:
        self._send(code, json.dumps(payload), "application/json", extra)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))

    def _form(self) -> dict[str, str]:
        parsed = urllib.parse.parse_qs(self._body().decode("utf-8", errors="replace"))
        return {k: v[0] for k, v in parsed.items()}

    def _json_body(self) -> dict | None:
        try:
            parsed = json.loads(self._body().decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    # ---- GET ------------------------------------------------------------------
    def do_GET(self) -> None:
        parts = urllib.parse.urlparse(self.path)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        route = parts.path
        try:
            if route == "/":
                conn = connect()
                rows = [dict(r) for r in conn.execute("SELECT * FROM products WHERE inventory > 0")]
                conn.close()
                self._html(
                    200,
                    page(
                        "Shop",
                        "<form action='/search' method='get'><input name='q'>"
                        "<button type='submit'>Search</button></form>" + product_forms(rows),
                    ),
                )
            elif route == "/search":
                rows, _ = search_products(params.get("q", ""))
                self._html(200, page("Search", product_forms(rows)))
            elif route == "/cart":
                user = params.get("user_id", "")
                conn = connect()
                lines = conn.execute(
                    "SELECT c.product_id, SUM(c.quantity) AS q, p.title FROM carts c "
                    "JOIN products p ON p.id=c.product_id WHERE c.user_id=? GROUP BY c.product_id",
                    (user,),
                ).fetchall()
                conn.close()
                items = "".join(f"<li>{html.escape(r['title'])} x {r['q']}</li>" for r in lines)
                self._html(
                    200,
                    page(
                        "Cart",
                        f"<ul>{items}</ul><form action='/checkout' method='post'>"
                        f"<input type='hidden' name='user_id' value='{html.escape(user)}'>"
                        f"<button type='submit'>Checkout</button></form>",
                    ),
                )
            elif route == "/orders":
                user = params.get("user_id", "")
                conn = connect()
                rows = conn.execute(
                    "SELECT o.*, p.title FROM orders o JOIN products p ON p.id=o.product_id "
                    "WHERE o.user_id=? ORDER BY o.id",
                    (user,),
                ).fetchall()
                conn.close()
                items = []
                for r in rows:
                    cancel = ""
                    if r["status"] == "completed":
                        cancel = (
                            f"<form action='/orders/cancel' method='post'>"
                            f"<input type='hidden' name='user_id' value='{html.escape(user)}'>"
                            f"<input type='hidden' name='order_id' value='{r['id']}'>"
                            f"<button type='submit'>Cancel</button></form>"
                        )
                    items.append(
                        f"<li>{html.escape(r['title'])} x {r['quantity']} — {r['status']}{cancel}</li>"
                    )
                self._html(200, page("Orders", "<ul>" + "".join(items) + "</ul>"))
            elif route == "/api/products":
                conn = connect()
                rows = [dict(r) for r in conn.execute("SELECT * FROM products")]
                conn.close()
                self._json(200, rows)
            elif re.fullmatch(r"/api/products/[^/]+", route):
                conn = connect()
                row = conn.execute("SELECT * FROM products WHERE id=?", (route.rsplit("/", 1)[1],)).fetchone()
                conn.close()
                if row is None:
                    self._json(404, {"error": "no such product"})
                else:
                    self._json(200, dict(row))
            elif route == "/api/search":
                rows, hit = search_products(params.get("q", ""))
                self._json(200, rows, {"X-Cache": "hit" if hit else "miss"})  # CACHE-HEADER
            elif route == "/api/orders":
                user = params.get("user_id", "")
                conn = connect()
                q = "SELECT * FROM orders WHERE user_id=? ORDER BY id"
                rows = [dict(r) for r in conn.execute(q, (user,))]
                conn.close()
                self._json(200, rows)
            elif route == "/api/metrics":
                conn = connect()
                sold = conn.execute(
                    "SELECT COALESCE(SUM(quantity),0) AS q, COALESCE(SUM(amount),0) AS a "
                    "FROM orders WHERE status='completed'"
                ).fetchone()
                by_status = {
                    r["status"]: r["c"]
                    for r in conn.execute("SELECT status, COUNT(*) AS c FROM orders GROUP BY status")
                }
                conn.close()
                self._json(
                    200,
                    {"units_sold": sold["q"], "revenue": sold["a"], "orders_by_status": by_status},
                )
            else:
                self._html(404, page("Not found", "<p>not found</p>"))
        except Exception as exc:  # keep the server alive; a route bug must not wedge the portal
            self._html(500, page("Error", f"<p>internal error: {html.escape(str(exc))}</p>"))

    # ---- POST -----------------------------------------------------------------
    def do_POST(self) -> None:
        route = urllib.parse.urlparse(self.path).path
        try:
            if route == "/cart/add":
                form = self._form()
                user, product = form.get("user_id", ""), form.get("product_id", "")
                if not user or not self._add_to_cart(user, product):
                    self._html(400, page("Error", "<p>user_id and a known product_id are required</p>"))
                    return
                self._redirect(f"/cart?user_id={urllib.parse.quote(user)}")
            elif route == "/checkout":
                user = self._form().get("user_id", "")
                ok, result = checkout(user)
                if ok:
                    self._redirect(f"/orders?user_id={urllib.parse.quote(user)}")
                else:
                    name = html.escape(str(result.get("title", result.get("id", "item"))))
                    pid = html.escape(str(result.get("id", "")))
                    self._html(
                        409,
                        page("Out of stock", f"<p>Sorry — {name} ({pid}) is out of stock.</p>"),
                    )
            elif route == "/orders/cancel":
                form = self._form()
                try:
                    order_id = int(form.get("order_id", ""))
                except ValueError:
                    self._html(400, page("Error", "<p>order_id must be an integer</p>"))
                    return
                code, payload = cancel_order(form.get("user_id", ""), order_id)
                if code == 200:
                    self._redirect(f"/orders?user_id={urllib.parse.quote(form.get('user_id', ''))}")
                else:
                    self._html(code, page("Cannot cancel", f"<p>{html.escape(payload['error'])}</p>"))
            elif route == "/api/cart/add":
                body = self._json_body()
                if not body or not body.get("user_id") or not body.get("product_id"):
                    self._json(400, {"error": "user_id and product_id are required"})
                    return
                if not self._add_to_cart(str(body["user_id"]), str(body["product_id"])):
                    self._json(404, {"error": "no such product"})
                    return
                self._json(201, {"ok": True})
            elif route == "/api/checkout":
                body = self._json_body()
                if not body or not body.get("user_id"):
                    self._json(400, {"error": "user_id is required"})
                    return
                ok, result = checkout(str(body["user_id"]))
                if ok:
                    self._json(201, {"order_ids": result})
                else:
                    self._json(
                        409,
                        {"error": "out of stock", "product": result.get("id"), "title": result.get("title")},
                    )
            elif re.fullmatch(r"/api/orders/\d+/cancel", route):
                body = self._json_body()
                if not body or not body.get("user_id"):
                    self._json(400, {"error": "user_id is required"})
                    return
                order_id = int(route.split("/")[3])
                code, payload = cancel_order(str(body["user_id"]), order_id)
                self._json(code, payload)
            else:
                self._json(404, {"error": "not found"})
        except Exception as exc:
            self._json(500, {"error": f"internal error: {exc}"})

    def _add_to_cart(self, user: str, product: str) -> bool:
        conn = connect()
        known = conn.execute("SELECT 1 FROM products WHERE id=?", (product,)).fetchone()
        if known is None:
            conn.close()
            return False
        conn.execute("INSERT INTO carts(user_id, product_id, quantity) VALUES(?,?,1)", (user, product))
        conn.commit()
        conn.close()
        return True


def main() -> None:
    if not PAYMENT_API_URL:
        sys.stderr.write("error: PAYMENT_API_URL is required\n")
        sys.exit(2)
    init_db()
    for _ in range(8):
        threading.Thread(target=worker_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "8000"))
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
