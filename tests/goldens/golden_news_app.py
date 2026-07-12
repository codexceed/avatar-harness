'''News analyzer — search news, get an AI summary + sentiment, stored in SQLite.

Configuration (environment variables):
  PORT             optional, default 8000 — the port the app serves on.
  NEWS_API_URL     required — URL of the news search API (returns gnews-shaped JSON).
  NEWS_API_KEY     required — API key sent to the news API as the `apikey` parameter.
  OPENAI_API_KEY   required — API key for the OpenAI chat-completions call.
  OPENAI_BASE_URL  optional — override the OpenAI API base URL (e.g. a local proxy).
'''

import html
import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from openai import OpenAI

_DB = "news.db"

_SEARCH_FORM = (
    '<form action="/search" method="get">'
    '<input type="text" name="q" placeholder="Search news"><button>Search</button></form>'
)


def _db():
    conn = sqlite3.connect(_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS analyses "
        "(id INTEGER PRIMARY KEY, title TEXT, url TEXT, summary TEXT, sentiment TEXT)"
    )
    return conn


def _fetch_articles(q):
    params = urllib.parse.urlencode({"q": q, "apikey": os.environ.get("NEWS_API_KEY", "")})
    url = os.environ["NEWS_API_URL"] + "?" + params
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8")).get("articles", [])


def _analyze(article):
    prompt = (
        "Reply with a JSON object with exactly two keys: 'summary' (a short string) and "
        "'sentiment' (one of positive, neutral, negative) for this article:\n"
        + json.dumps(article)
    )
    reply = OpenAI().chat.completions.create(
        model="gpt-4.1-nano", messages=[{"role": "user", "content": prompt}]
    )
    parsed = json.loads(reply.choices[0].message.content)
    return parsed["summary"], parsed["sentiment"]


def _store(title, url, summary, sentiment):
    conn = _db()
    conn.execute(
        "INSERT INTO analyses (title, url, summary, sentiment) VALUES (?, ?, ?, ?)",
        (title, url, summary, sentiment),
    )
    conn.commit()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _home_page(self):
        rows = _db().execute("SELECT title, summary, sentiment FROM analyses").fetchall()
        items = "".join(
            f"<li>{html.escape(t)}: {html.escape(s)} ({html.escape(sent)})</li>"
            for t, s, sent in rows
        )
        return (
            "<html><body><h1>News analyzer</h1>"
            f"{_SEARCH_FORM}<h2>Analyses</h2><ul>{items}</ul></body></html>"
        )

    def _search_page(self, q):
        try:
            articles = _fetch_articles(q)
        except (ValueError, OSError):
            return (
                "<html><body><p>Could not reach the news API — check the "
                "NEWS_API_URL configuration.</p></body></html>"
            )
        items = []
        for a in articles:
            fields = "".join(
                f'<input type="hidden" name="{k}" value="{html.escape(a.get(k, ""))}">'
                for k in ("title", "url", "content")
            )
            items.append(
                f'<li>{html.escape(a.get("title", ""))}'
                f'<form action="/analyze" method="post">{fields}'
                "<button>Analyze</button></form></li>"
            )
        return f"<html><body>{_SEARCH_FORM}<ul>{''.join(items)}</ul></body></html>"

    def do_GET(self):
        parts = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parts.query).get("q", [""])[0]
        if parts.path == "/api/articles":
            try:
                self._send(200, json.dumps(_fetch_articles(q)))
            except (ValueError, OSError):
                self._send(502, json.dumps({"error": "news API unreachable or invalid (NEWS_API_URL)"}))
        elif parts.path == "/api/analyses":
            rows = _db().execute("SELECT title, url, summary, sentiment FROM analyses").fetchall()
            keys = ("title", "url", "summary", "sentiment")
            self._send(200, json.dumps([dict(zip(keys, r)) for r in rows]))
        elif parts.path == "/search":
            self._send(200, self._search_page(q), "text/html")
        elif parts.path == "/":
            self._send(200, self._home_page(), "text/html")
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)).decode("utf-8")
        if self.path == "/api/analyses":
            try:
                body = json.loads(raw)
            except ValueError:
                self._send(400, json.dumps({"error": "bad request"}))
                return
            summary, sentiment = _analyze(body)
            _store(body.get("title", ""), body.get("url", ""), summary, sentiment)
            record = {
                "title": body.get("title", ""),
                "url": body.get("url", ""),
                "summary": summary,
                "sentiment": sentiment,
            }
            self._send(201, json.dumps(record))
        elif self.path == "/analyze":
            fields = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
            summary, sentiment = _analyze(fields)
            _store(fields.get("title", ""), fields.get("url", ""), summary, sentiment)
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send(404, json.dumps({"error": "not found"}))


if __name__ == "__main__":
    for _required in ("NEWS_API_URL", "NEWS_API_KEY"):
        if not os.environ.get(_required):
            sys.stderr.write("error: " + _required + " is required\n")
            sys.exit(2)
    port = int(os.environ.get("PORT", "8000"))
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
