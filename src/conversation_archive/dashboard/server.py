#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sqlite3
import sys
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from ..chatgpt.paths import default_db_path

UTC = dt.timezone.utc
RANGE_MONTHS = {
    "12m": 12,
    "6m": 6,
    "3m": 3,
    "1m": 1,
    "year": 12,
    "quarter": 3,
    "month": 1,
}
WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _jsonable_row(row: sqlite3.Row) -> dict[str, object]:
    return {key: row[key] for key in row.keys()}


def _fmt_int(value: object) -> str:
    try:
        return f"{int(value or 0):,}"
    except Exception:
        return "0"


def _fmt_pct(value: float) -> str:
    return f"{value:+.0f}%"


def _dt_from_ts(ts: float | None) -> dt.datetime | None:
    if ts is None:
        return None
    try:
        return dt.datetime.fromtimestamp(float(ts), tz=UTC)
    except Exception:
        return None


def _ts_from_date(value: str, *, end_of_day: bool = False) -> float | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=UTC)
        if end_of_day:
            parsed = parsed + dt.timedelta(days=1) - dt.timedelta(seconds=1)
        return parsed.timestamp()
    except Exception:
        return None


def _shift_months(value: dt.datetime, months: int) -> dt.datetime:
    year = value.year
    month = value.month + months
    while month < 1:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    days_in_month = [
        31,
        29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ][month - 1]
    return value.replace(year=year, month=month, day=min(value.day, days_in_month))


HTML_TEMPLATE = """<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>ChatGPT Reflection Dashboard</title>
    <style>
      :root {{
        color-scheme: light;
        --bg: #f7f7f4;
        --panel: #ffffff;
        --ink: #1d1d1b;
        --muted: #666b70;
        --line: #deded8;
        --soft: #ededeb;
        --accent: #9b4f2f;
        --accent-2: #276063;
        --danger: #8f3e44;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
        background: var(--bg);
        color: var(--ink);
        line-height: 1.42;
      }}
      a {{ color: inherit; }}
      a:hover {{ color: var(--accent); }}
      button, input, select {{
        font: inherit;
      }}
      input[type=text], input[type=date], input[type=number], select {{
        border: 1px solid var(--line);
        border-radius: 6px;
        padding: 8px 10px;
        background: #fff;
        color: var(--ink);
      }}
      button, .button {{
        display: inline-flex;
        align-items: center;
        justify-content: center;
        border: 1px solid var(--line);
        border-radius: 6px;
        padding: 8px 11px;
        min-height: 36px;
        background: #fff;
        color: var(--ink);
        text-decoration: none;
        cursor: pointer;
      }}
      button.primary, .button.primary {{
        border-color: var(--accent);
        background: var(--accent);
        color: #fff;
      }}
      .app-shell {{
        max-width: 1480px;
        margin: 0 auto;
        padding: 20px;
      }}
      .topbar {{
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 16px;
        margin-bottom: 18px;
      }}
      .nav {{
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
      }}
      h1, h2, h3 {{
        margin: 0;
        line-height: 1.15;
      }}
      h1 {{ font-size: 26px; }}
      h2 {{ font-size: 18px; }}
      h3 {{ font-size: 15px; }}
      .subtle, .hint, .meta {{
        color: var(--muted);
        font-size: 13px;
      }}
      .toolbar {{
        display: flex;
        gap: 8px;
        align-items: center;
        flex-wrap: wrap;
        margin: 12px 0 18px;
      }}
      .range-link {{
        border: 1px solid var(--line);
        border-radius: 999px;
        padding: 6px 10px;
        text-decoration: none;
        background: #fff;
        font-size: 13px;
      }}
      .range-link.active {{
        background: var(--ink);
        border-color: var(--ink);
        color: #fff;
      }}
      .grid {{
        display: grid;
        grid-template-columns: 1.45fr 0.9fr;
        gap: 14px;
        align-items: start;
      }}
      .panel {{
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 16px;
      }}
      .metric-grid {{
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 10px;
        margin-bottom: 14px;
      }}
      .metric {{
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 12px;
        min-width: 0;
      }}
      .metric .label {{
        color: var(--muted);
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0;
      }}
      .metric .value {{
        margin-top: 5px;
        font-size: 24px;
        line-height: 1.1;
        font-weight: 650;
        overflow-wrap: anywhere;
      }}
      .metric .delta {{
        margin-top: 5px;
        color: var(--muted);
        font-size: 12px;
      }}
      .section-head {{
        display: flex;
        justify-content: space-between;
        gap: 12px;
        align-items: baseline;
        margin-bottom: 12px;
      }}
      .chart {{
        width: 100%;
        min-height: 250px;
      }}
      .topic-row {{
        display: grid;
        grid-template-columns: minmax(120px, 1fr) 3fr auto;
        gap: 10px;
        align-items: center;
        padding: 9px 0;
        border-top: 1px solid var(--soft);
      }}
      .bar-track {{
        height: 11px;
        background: var(--soft);
        border-radius: 999px;
        overflow: hidden;
      }}
      .bar-fill {{
        height: 100%;
        background: var(--accent);
      }}
      .bar-fill.sensitive {{
        background: var(--danger);
      }}
      .timeline {{
        display: grid;
        gap: 8px;
      }}
      .timeline-row {{
        display: grid;
        grid-template-columns: 84px 1fr;
        gap: 10px;
        align-items: baseline;
        border-top: 1px solid var(--soft);
        padding-top: 8px;
      }}
      .lens-grid {{
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 8px;
      }}
      .lens {{
        display: block;
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 10px;
        text-decoration: none;
        background: #fff;
      }}
      .lens strong {{
        display: block;
        margin-bottom: 4px;
      }}
      table {{
        width: 100%;
        border-collapse: collapse;
      }}
      th, td {{
        border-top: 1px solid var(--soft);
        padding: 9px 6px;
        text-align: left;
        vertical-align: top;
        font-size: 14px;
      }}
      th {{
        color: var(--muted);
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0;
      }}
      pre {{
        white-space: pre-wrap;
        background: #fbfbfa;
        padding: 10px;
        border: 1px solid var(--line);
        border-radius: 6px;
        overflow-x: auto;
      }}
      details {{
        background: #fff;
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 10px 12px;
        margin: 10px 0;
      }}
      summary {{ cursor: pointer; }}
      mark {{ background: #ffe08a; padding: 0 2px; border-radius: 3px; }}
      code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
      .empty {{
        border: 1px dashed var(--line);
        border-radius: 8px;
        padding: 16px;
        background: #fff;
      }}
      .search-form {{
        display: grid;
        grid-template-columns: minmax(220px, 1fr) auto auto auto;
        gap: 8px;
        align-items: center;
      }}
      @media (max-width: 980px) {{
        .grid, .metric-grid, .lens-grid {{
          grid-template-columns: 1fr;
        }}
        .topbar {{
          display: block;
        }}
        .search-form {{
          grid-template-columns: 1fr;
        }}
      }}
    </style>
  </head>
  <body>
    <main class="app-shell">
      <div class="topbar">
        <div>
          <h1>ChatGPT Reflection Dashboard</h1>
          <div class="subtle">Local export analytics. Metrics and drilldowns stay on this machine.</div>
        </div>
        <nav class="nav">
          <a class="button" href="/">Dashboard</a>
          <a class="button" href="/search">Search</a>
          <a class="button" href="/api/overview">API</a>
        </nav>
      </div>
      {body}
    </main>
  </body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    db_path: Path = Path(default_db_path())

    def do_GET(self) -> None:
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        path = url.path

        try:
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if path == "/":
                self._send_page(self._dashboard_body(qs))
                return
            if path == "/search":
                self._send_page(self._search_body(qs))
                return
            if path == "/conv":
                self._send_page(self._conversation_body(qs))
                return
            if path == "/api/overview":
                self._send_json(self._overview_data(qs))
                return
            if path == "/api/activity":
                self._send_json(self._activity_data(qs))
                return
            if path == "/api/topics":
                self._send_json(self._topics_data(qs))
                return
            if path == "/api/conversations":
                self._send_json(self._conversations_data(qs))
                return
            if path == "/api/reflections":
                self._send_json(self._reflections_data(qs))
                return
            if path == "/api/lenses":
                self._send_json(self._lenses_data())
                return

            self.send_error(404, "Not Found")
        except Exception as e:
            if path.startswith("/api/"):
                self._send_json({"error": str(e)}, status=500)
                return
            self._send_page(f"<pre>Error: {html.escape(str(e))}</pre>", status=500)

    def _send_page(self, body: str, *, status: int = 200) -> None:
        page = HTML_TEMPLATE.format(body=body)
        data = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: object, *, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        return

    def _reflection_ready(self, cur: sqlite3.Cursor) -> bool:
        return bool(
            cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_metrics'"
            ).fetchone()
        )

    def _setup_required_html(self) -> str:
        return """
          <section class="empty">
            <h2>Build the reflection index</h2>
            <p class="subtle">The search index exists, but the dashboard needs derived metrics and topic tables.</p>
            <pre>conversation-archive-build-reflection-indexes --reset</pre>
          </section>
        """

    def _latest_create_time(self, cur: sqlite3.Cursor) -> float | None:
        row = cur.execute("SELECT MAX(create_time) AS latest FROM conversation_metrics").fetchone()
        return float(row["latest"]) if row and row["latest"] is not None else None

    def _range_info(self, cur: sqlite3.Cursor, qs: dict[str, list[str]]) -> dict[str, object]:
        raw = (qs.get("range") or ["all"])[0].strip().lower()
        range_key = raw if raw in {"all", "custom", *RANGE_MONTHS.keys()} else "all"
        latest_ts = self._latest_create_time(cur)
        latest_dt = _dt_from_ts(latest_ts)
        start_ts: float | None = None
        end_ts: float | None = latest_ts

        if range_key == "custom":
            start_ts = _ts_from_date((qs.get("start") or [""])[0])
            end_ts = _ts_from_date((qs.get("end") or [""])[0], end_of_day=True) or latest_ts
            label = f"{(qs.get('start') or [''])[0] or 'start'} to {(qs.get('end') or [''])[0] or 'latest'}"
        elif range_key == "all":
            label = "All time"
            end_ts = None
        else:
            months = RANGE_MONTHS[range_key]
            start_dt = _shift_months(latest_dt, -months) if latest_dt else None
            start_ts = start_dt.timestamp() if start_dt else None
            label = {"year": "Past year", "quarter": "Past quarter", "month": "Past month"}.get(
                range_key, f"Past {range_key}"
            )

        prev_start: float | None = None
        prev_end: float | None = None
        if start_ts is not None and end_ts is not None and end_ts > start_ts:
            span = end_ts - start_ts
            prev_start = start_ts - span
            prev_end = start_ts

        return {
            "key": range_key,
            "label": label,
            "start": start_ts,
            "end": end_ts,
            "previous_start": prev_start,
            "previous_end": prev_end,
        }

    @staticmethod
    def _time_where(alias: str, info: dict[str, object]) -> tuple[str, list[object]]:
        clauses: list[str] = []
        params: list[object] = []
        start = info.get("start")
        end = info.get("end")
        if start is not None:
            clauses.append(f"{alias}.create_time >= ?")
            params.append(start)
        if end is not None:
            clauses.append(f"{alias}.create_time <= ?")
            params.append(end)
        return (" AND ".join(clauses) if clauses else "1=1", params)

    @staticmethod
    def _specific_time_where(alias: str, start: object, end: object) -> tuple[str, list[object]]:
        clauses: list[str] = []
        params: list[object] = []
        if start is not None:
            clauses.append(f"{alias}.create_time >= ?")
            params.append(start)
        if end is not None:
            clauses.append(f"{alias}.create_time < ?")
            params.append(end)
        return (" AND ".join(clauses) if clauses else "1=1", params)

    def _overview_data(self, qs: dict[str, list[str]]) -> dict[str, object]:
        conn = _connect(self.db_path)
        cur = conn.cursor()
        if not self._reflection_ready(cur):
            return {"ready": False, "error": "Run conversation-archive-build-reflection-indexes first."}
        info = self._range_info(cur, qs)
        where_sql, params = self._time_where("m", info)
        overview = cur.execute(
            f"""
            SELECT
              COUNT(*) AS conversations,
              COALESCE(SUM(user_message_count), 0) AS user_messages,
              COALESCE(SUM(assistant_message_count), 0) AS assistant_messages,
              COALESCE(SUM(tool_message_count), 0) AS tool_messages,
              COALESCE(SUM(message_count), 0) AS messages,
              COALESCE(SUM(text_chars), 0) AS text_chars,
              COUNT(DISTINCT substr(create_time_iso, 1, 10)) AS active_days,
              MIN(create_time_iso) AS first_seen,
              MAX(update_time_iso) AS last_seen
            FROM conversation_metrics m
            WHERE {where_sql}
            """,
            params,
        ).fetchone()
        previous = None
        if info.get("previous_start") is not None and info.get("previous_end") is not None:
            prev_where, prev_params = self._specific_time_where(
                "m", info.get("previous_start"), info.get("previous_end")
            )
            previous = cur.execute(
                f"""
                SELECT COUNT(*) AS conversations, COALESCE(SUM(user_message_count), 0) AS user_messages
                FROM conversation_metrics m
                WHERE {prev_where}
                """,
                prev_params,
            ).fetchone()
        hour_where, hour_params = self._time_where("m", info)
        peak_hour = cur.execute(
            f"""
            SELECT strftime('%H', msg.create_time_iso) AS hour, COUNT(*) AS messages
            FROM messages msg
            JOIN conversation_metrics m ON m.conversation_id = msg.conversation_id
            WHERE msg.role = 'user' AND {hour_where}
            GROUP BY hour
            ORDER BY messages DESC
            LIMIT 1
            """,
            hour_params,
        ).fetchone()
        peak_day = cur.execute(
            f"""
            SELECT strftime('%w', msg.create_time_iso) AS dow, COUNT(DISTINCT msg.conversation_id) AS conversations
            FROM messages msg
            JOIN conversation_metrics m ON m.conversation_id = msg.conversation_id
            WHERE msg.role = 'user' AND {hour_where}
            GROUP BY dow
            ORDER BY conversations DESC
            LIMIT 1
            """,
            hour_params,
        ).fetchone()
        long_topics = cur.execute(
            f"""
            SELECT t.topic, COUNT(*) AS conversations, AVG(m.duration_minutes) AS avg_minutes
            FROM conversation_topics t
            JOIN conversation_metrics m ON m.conversation_id = t.conversation_id
            WHERE {where_sql}
            GROUP BY t.topic
            ORDER BY avg_minutes DESC
            LIMIT 5
            """,
            params,
        ).fetchall()
        current_conversations = int(overview["conversations"] or 0)
        previous_conversations = int(previous["conversations"] or 0) if previous else 0
        delta = None
        if previous_conversations:
            delta = ((current_conversations - previous_conversations) / previous_conversations) * 100.0
        return {
            "ready": True,
            "range": info,
            "overview": _jsonable_row(overview),
            "previous": _jsonable_row(previous) if previous else None,
            "conversation_delta_pct": delta,
            "peak_hour": _jsonable_row(peak_hour) if peak_hour else None,
            "peak_day": {
                "weekday": WEEKDAYS[int(peak_day["dow"])] if peak_day and peak_day["dow"] is not None else None,
                "conversations": peak_day["conversations"] if peak_day else 0,
            },
            "long_running_topics": [_jsonable_row(row) for row in long_topics],
        }

    def _activity_data(self, qs: dict[str, list[str]]) -> dict[str, object]:
        conn = _connect(self.db_path)
        cur = conn.cursor()
        if not self._reflection_ready(cur):
            return {"ready": False, "items": []}
        info = self._range_info(cur, qs)
        bucket = (qs.get("bucket") or ["month"])[0].strip().lower()
        bucket_expr = {
            "day": "substr(m.create_time_iso, 1, 10)",
            "week": "strftime('%Y-W%W', m.create_time_iso)",
            "month": "substr(m.create_time_iso, 1, 7)",
        }.get(bucket, "substr(m.create_time_iso, 1, 7)")
        where_sql, params = self._time_where("m", info)
        rows = cur.execute(
            f"""
            SELECT
              {bucket_expr} AS bucket,
              COUNT(*) AS conversations,
              COALESCE(SUM(user_message_count), 0) AS user_messages,
              COALESCE(SUM(assistant_message_count), 0) AS assistant_messages
            FROM conversation_metrics m
            WHERE {where_sql}
            GROUP BY bucket
            ORDER BY bucket
            """,
            params,
        ).fetchall()
        return {"ready": True, "range": info, "bucket": bucket, "items": [_jsonable_row(row) for row in rows]}

    def _topics_data(self, qs: dict[str, list[str]]) -> dict[str, object]:
        conn = _connect(self.db_path)
        cur = conn.cursor()
        if not self._reflection_ready(cur):
            return {"ready": False, "items": []}
        info = self._range_info(cur, qs)
        where_sql, params = self._time_where("m", info)
        rows = cur.execute(
            f"""
            SELECT
              t.topic,
              MAX(t.is_sensitive) AS is_sensitive,
              COUNT(DISTINCT t.conversation_id) AS conversations,
              AVG(t.score) AS avg_score,
              GROUP_CONCAT(t.evidence_terms, '|') AS evidence_blob
            FROM conversation_topics t
            JOIN conversation_metrics m ON m.conversation_id = t.conversation_id
            WHERE {where_sql}
            GROUP BY t.topic
            ORDER BY conversations DESC, avg_score DESC
            """,
            params,
        ).fetchall()
        total = sum(int(row["conversations"] or 0) for row in rows) or 1
        items: list[dict[str, object]] = []
        for row in rows:
            terms: list[str] = []
            if not row["is_sensitive"]:
                counter: Counter[str] = Counter()
                for chunk in str(row["evidence_blob"] or "").split("|"):
                    try:
                        counter.update(json.loads(chunk))
                    except Exception:
                        continue
                terms = [term for term, _ in counter.most_common(5)]
            items.append(
                {
                    "topic": row["topic"],
                    "is_sensitive": bool(row["is_sensitive"]),
                    "conversations": row["conversations"],
                    "share": float(row["conversations"] or 0) / total,
                    "avg_score": row["avg_score"],
                    "evidence_terms": terms,
                }
            )
        return {"ready": True, "range": info, "items": items}

    def _conversations_data(self, qs: dict[str, list[str]]) -> dict[str, object]:
        conn = _connect(self.db_path)
        cur = conn.cursor()
        if not self._reflection_ready(cur):
            return {"ready": False, "items": []}
        info = self._range_info(cur, qs)
        q = (qs.get("q") or [""])[0].strip()
        topic = (qs.get("topic") or [""])[0].strip()
        sort = (qs.get("sort") or ["latest"])[0].strip()
        page = self._parse_int(qs, "page", default=1, min_value=1, max_value=10000)
        limit = self._parse_int(qs, "limit", default=40, min_value=1, max_value=200)
        offset = (page - 1) * limit
        where_sql, params = self._time_where("m", info)
        topic_join = ""
        topic_where = ""
        if topic:
            topic_join = "JOIN conversation_topics t ON t.conversation_id = m.conversation_id"
            topic_where = " AND t.topic = ?"
            params.append(topic)
        order_sql = {
            "oldest": "m.create_time ASC",
            "messages": "m.message_count DESC, m.update_time DESC",
            "title": "LOWER(m.title) ASC",
        }.get(sort, "m.update_time DESC")

        if q:
            rows = cur.execute(
                f"""
                WITH hits AS (
                  SELECT msg.conversation_id, COUNT(*) AS hits
                  FROM messages_fts
                  JOIN messages msg ON msg.id = messages_fts.rowid
                  WHERE messages_fts MATCH ?
                  GROUP BY msg.conversation_id
                )
                SELECT
                  m.conversation_id, m.title, m.create_time_iso, m.update_time_iso,
                  m.user_message_count, m.assistant_message_count, m.message_count,
                  hits.hits, NULL AS score
                FROM hits
                JOIN conversation_metrics m ON m.conversation_id = hits.conversation_id
                {topic_join}
                WHERE {where_sql}{topic_where}
                ORDER BY hits.hits DESC, {order_sql}
                LIMIT ? OFFSET ?
                """,
                [q, *params, limit, offset],
            ).fetchall()
        else:
            rows = cur.execute(
                f"""
                SELECT
                  m.conversation_id, m.title, m.create_time_iso, m.update_time_iso,
                  m.user_message_count, m.assistant_message_count, m.message_count,
                  NULL AS hits, NULL AS score
                FROM conversation_metrics m
                {topic_join}
                WHERE {where_sql}{topic_where}
                ORDER BY {order_sql}
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return {
            "ready": True,
            "range": info,
            "query": q,
            "topic": topic,
            "page": page,
            "limit": limit,
            "items": [_jsonable_row(row) for row in rows],
        }

    def _reflections_data(self, qs: dict[str, list[str]]) -> dict[str, object]:
        conn = _connect(self.db_path)
        cur = conn.cursor()
        if not self._reflection_ready(cur):
            return {"ready": False}
        requested = (qs.get("range") or ["all"])[0].strip().lower()
        aliases = {"year": "12m", "quarter": "3m", "month": "1m"}
        requested = aliases.get(requested, requested)
        range_key = requested if requested in {"all", "12m", "6m", "3m", "1m"} else "all"
        row = cur.execute(
            """
            SELECT range_key, summary, bullets_json, evidence_json, source_kind, model, generated_at
            FROM reflection_summaries
            WHERE range_key = ?
            """,
            (range_key,),
        ).fetchone()
        if not row:
            return {"ready": True, "range_key": range_key, "summary": None}
        return {
            "ready": True,
            "range_key": row["range_key"],
            "summary": row["summary"],
            "bullets": json.loads(row["bullets_json"] or "[]"),
            "evidence": json.loads(row["evidence_json"] or "[]"),
            "source_kind": row["source_kind"],
            "model": row["model"],
            "generated_at": row["generated_at"],
        }

    def _lenses_data(self) -> dict[str, object]:
        conn = _connect(self.db_path)
        cur = conn.cursor()
        if not cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dashboard_lenses'"
        ).fetchone():
            return {"ready": False, "items": []}
        rows = cur.execute("SELECT name, query, description FROM dashboard_lenses ORDER BY name").fetchall()
        return {"ready": True, "items": [_jsonable_row(row) for row in rows]}

    def _dashboard_body(self, qs: dict[str, list[str]]) -> str:
        conn = _connect(self.db_path)
        cur = conn.cursor()
        if not self._reflection_ready(cur):
            return self._setup_required_html()

        overview_data = self._overview_data(qs)
        topics_data = self._topics_data(qs)
        activity_data = self._activity_data({"range": qs.get("range") or ["all"], "bucket": ["month"]})
        reflections_data = self._reflections_data({"range": qs.get("range") or ["all"]})
        lenses_data = self._lenses_data()
        timeline = self._topic_timeline(cur, self._range_info(cur, qs))
        overview = overview_data["overview"]  # type: ignore[index]
        info = overview_data["range"]  # type: ignore[index]
        range_key = str(info["key"])  # type: ignore[index]
        delta = overview_data.get("conversation_delta_pct")  # type: ignore[union-attr]

        range_links = self._range_links(range_key)
        custom_form = self._custom_range_form(qs)
        metrics = [
            ("Conversations", _fmt_int(overview["conversations"]), _fmt_pct(delta) + " vs prior" if isinstance(delta, float) else "No prior range"),
            ("User messages", _fmt_int(overview["user_messages"]), "Prompts and follow-ups"),
            ("Active days", _fmt_int(overview["active_days"]), str(info["label"])),
            ("Peak time", self._peak_time_label(overview_data), self._peak_day_label(overview_data)),
        ]

        out: list[str] = [
            f"<div class='toolbar'>{range_links}{custom_form}</div>",
            "<section class='metric-grid'>",
        ]
        for label, value, detail in metrics:
            out.append(
                f"<div class='metric'><div class='label'>{html.escape(label)}</div>"
                f"<div class='value'>{html.escape(value)}</div><div class='delta'>{html.escape(detail)}</div></div>"
            )
        out.append("</section>")
        out.append("<section class='grid'>")
        out.append("<div>")
        out.append(
            "<section class='panel'><div class='section-head'><div><h2>Activity over time</h2>"
            "<div class='subtle'>Conversation starts and message volume by month.</div></div>"
            "<a class='button' href='/api/activity'>JSON</a></div>"
        )
        out.append(self._activity_svg(activity_data["items"]))  # type: ignore[index]
        out.append("</section>")
        out.append("<section class='panel' style='margin-top:14px;'><div class='section-head'><div><h2>Topic share</h2><div class='subtle'>Sensitive topics are aggregated by default.</div></div><a class='button' href='/api/topics'>JSON</a></div>")
        out.append(self._topic_bars(topics_data["items"]))  # type: ignore[index]
        out.append("</section>")
        out.append("<section class='panel' style='margin-top:14px;'><div class='section-head'><div><h2>Major themes timeline</h2><div class='subtle'>Top detected themes by month.</div></div></div>")
        out.append(self._timeline_html(timeline))
        out.append("</section>")
        out.append("</div>")
        out.append("<aside>")
        out.append(self._reflection_html(reflections_data))
        out.append(self._lenses_html(lenses_data))
        out.append(self._long_running_topics_html(overview_data.get("long_running_topics", [])))
        out.append("</aside>")
        out.append("</section>")
        return "\n".join(out)

    def _range_links(self, active: str) -> str:
        ranges = [
            ("all", "All time"),
            ("year", "Year"),
            ("quarter", "Quarter"),
            ("month", "Month"),
            ("12m", "12m"),
            ("6m", "6m"),
            ("3m", "3m"),
            ("1m", "1m"),
        ]
        links = []
        for key, label in ranges:
            cls = "range-link active" if active == key else "range-link"
            links.append(f"<a class='{cls}' href='/?range={key}'>{label}</a>")
        return "".join(links)

    def _custom_range_form(self, qs: dict[str, list[str]]) -> str:
        start = html.escape((qs.get("start") or [""])[0])
        end = html.escape((qs.get("end") or [""])[0])
        return (
            "<form method='GET' action='/' style='display:flex; gap:6px; flex-wrap:wrap; align-items:center;'>"
            "<input type='hidden' name='range' value='custom' />"
            f"<input type='date' name='start' value='{start}' />"
            f"<input type='date' name='end' value='{end}' />"
            "<button type='submit'>Apply</button>"
            "</form>"
        )

    def _peak_time_label(self, overview_data: dict[str, object]) -> str:
        peak = overview_data.get("peak_hour")
        if not isinstance(peak, dict) or peak.get("hour") is None:
            return "None"
        return f"{int(str(peak['hour']))}:00"

    def _peak_day_label(self, overview_data: dict[str, object]) -> str:
        peak = overview_data.get("peak_day")
        if not isinstance(peak, dict) or not peak.get("weekday"):
            return "No peak day"
        return f"{peak['weekday']} peak day"

    def _activity_svg(self, items: object) -> str:
        rows = list(items) if isinstance(items, list) else []
        if not rows:
            return "<div class='empty'>No activity in this range.</div>"
        width = 900
        height = 260
        pad_left = 42
        pad_bottom = 34
        chart_w = width - pad_left - 18
        chart_h = height - 28 - pad_bottom
        max_value = max(int(row.get("conversations", 0) or 0) for row in rows) or 1
        count = len(rows)
        step = chart_w / max(1, count - 1)
        points: list[str] = []
        labels: list[str] = []
        bars: list[str] = []
        for idx, row in enumerate(rows):
            x = pad_left + (idx * step if count > 1 else chart_w / 2)
            value = int(row.get("conversations", 0) or 0)
            y = 20 + chart_h - (value / max_value) * chart_h
            points.append(f"{x:.1f},{y:.1f}")
            bar_w = max(4.0, chart_w / max(1, count) * 0.55)
            bars.append(
                f"<rect x='{x - bar_w / 2:.1f}' y='{y:.1f}' width='{bar_w:.1f}' height='{20 + chart_h - y:.1f}' fill='#d8c1b4' rx='2'><title>{html.escape(str(row.get('bucket')))}: {value} conversations</title></rect>"
            )
            if idx in {0, count - 1} or (count > 8 and idx % max(1, count // 5) == 0):
                anchor = "start" if idx == 0 else "end" if idx == count - 1 else "middle"
                labels.append(
                    f"<text x='{x:.1f}' y='{height - 8}' text-anchor='{anchor}' font-size='11' fill='#666b70'>{html.escape(str(row.get('bucket')))}</text>"
                )
        polyline = " ".join(points)
        return (
            f"<svg class='chart' viewBox='0 0 {width} {height}' role='img' aria-label='Activity chart'>"
            f"<line x1='{pad_left}' y1='{20 + chart_h}' x2='{width - 10}' y2='{20 + chart_h}' stroke='#deded8' />"
            f"<line x1='{pad_left}' y1='20' x2='{pad_left}' y2='{20 + chart_h}' stroke='#deded8' />"
            f"{''.join(bars)}"
            f"<polyline fill='none' stroke='#9b4f2f' stroke-width='3' points='{polyline}' />"
            f"{''.join(labels)}"
            f"</svg>"
        )

    def _topic_bars(self, items: object) -> str:
        rows = list(items) if isinstance(items, list) else []
        if not rows:
            return "<div class='empty'>No topics detected in this range.</div>"
        max_count = max(int(row.get("conversations", 0) or 0) for row in rows) or 1
        out: list[str] = []
        for row in rows[:10]:
            topic = str(row.get("topic") or "")
            count = int(row.get("conversations", 0) or 0)
            width = max(3, round((count / max_count) * 100))
            sensitive = bool(row.get("is_sensitive"))
            query = urlencode({"topic": topic, "sort": "messages"})
            terms = "Aggregated sensitive category" if sensitive else ", ".join(row.get("evidence_terms") or [])
            out.append(
                "<div class='topic-row'>"
                f"<a href='/search?{query}'><strong>{html.escape(topic)}</strong></a>"
                f"<div><div class='bar-track'><div class='bar-fill {'sensitive' if sensitive else ''}' style='width:{width}%'></div></div>"
                f"<div class='subtle'>{html.escape(terms)}</div></div>"
                f"<div class='meta'>{count:,}</div>"
                "</div>"
            )
        return "\n".join(out)

    def _topic_timeline(self, cur: sqlite3.Cursor, info: dict[str, object]) -> list[dict[str, object]]:
        where_sql, params = self._time_where("m", info)
        rows = cur.execute(
            f"""
            SELECT substr(m.create_time_iso, 1, 7) AS month, t.topic, COUNT(*) AS conversations
            FROM conversation_metrics m
            JOIN conversation_topics t ON t.conversation_id = m.conversation_id
            WHERE {where_sql}
            GROUP BY month, t.topic
            ORDER BY month DESC, conversations DESC
            """,
            params,
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(row["month"], []).append(row)
        out: list[dict[str, object]] = []
        for month in sorted(grouped.keys(), reverse=True)[:12]:
            top = grouped[month][:3]
            out.append({"month": month, "topics": [_jsonable_row(row) for row in top]})
        return out

    def _timeline_html(self, rows: list[dict[str, object]]) -> str:
        if not rows:
            return "<div class='empty'>No monthly topic history for this range.</div>"
        out = ["<div class='timeline'>"]
        for row in rows:
            topics = row.get("topics") or []
            text = ", ".join(
                f"{topic['topic']} ({topic['conversations']})" for topic in topics  # type: ignore[index]
            )
            out.append(
                f"<div class='timeline-row'><div class='meta'>{html.escape(str(row.get('month')))}</div><div>{html.escape(text)}</div></div>"
            )
        out.append("</div>")
        return "\n".join(out)

    def _reflection_html(self, data: dict[str, object]) -> str:
        if not data.get("ready") or not data.get("summary"):
            return "<section class='panel'><h2>Reflection</h2><div class='empty'>No cached reflection summary yet.</div></section>"
        bullets = data.get("bullets") if isinstance(data.get("bullets"), list) else []
        evidence = data.get("evidence") if isinstance(data.get("evidence"), list) else []
        out = [
            "<section class='panel'>",
            "<div class='section-head'><div><h2>Reflection</h2><div class='subtle'>Cached deterministic summary with source links.</div></div></div>",
            f"<p>{html.escape(str(data.get('summary')))}</p>",
            "<ul>",
        ]
        for bullet in bullets:
            out.append(f"<li>{html.escape(str(bullet))}</li>")
        out.append("</ul>")
        if evidence:
            out.append("<h3>Evidence conversations</h3><table><tbody>")
            for item in evidence[:5]:
                if not isinstance(item, dict):
                    continue
                href = f"/conv?conversation_id={html.escape(str(item.get('conversation_id') or ''))}"
                out.append(
                    f"<tr><td><a href='{href}'>{html.escape(str(item.get('title') or 'Untitled'))}</a>"
                    f"<div class='meta'>{html.escape(str(item.get('create_time_iso') or ''))}</div></td>"
                    f"<td class='meta'>{html.escape(str(item.get('messages') or 0))} msgs</td></tr>"
                )
            out.append("</tbody></table>")
        out.append(f"<div class='meta'>Generated: {html.escape(str(data.get('generated_at') or ''))}</div>")
        out.append("</section>")
        return "\n".join(out)

    def _lenses_html(self, data: dict[str, object]) -> str:
        rows = data.get("items") if isinstance(data.get("items"), list) else []
        out = [
            "<section class='panel' style='margin-top:14px;'>",
            "<div class='section-head'><div><h2>Saved lenses</h2><div class='subtle'>Reusable searches for collaboration patterns.</div></div></div>",
            "<div class='lens-grid'>",
        ]
        for row in rows:
            if not isinstance(row, dict):
                continue
            href = "/search?" + urlencode({"q": str(row.get("query") or "")})
            out.append(
                f"<a class='lens' href='{href}'><strong>{html.escape(str(row.get('name') or ''))}</strong>"
                f"<span class='subtle'>{html.escape(str(row.get('description') or ''))}</span></a>"
            )
        out.append("</div></section>")
        return "\n".join(out)

    def _long_running_topics_html(self, rows: object) -> str:
        topics = list(rows) if isinstance(rows, list) else []
        if not topics:
            return ""
        out = [
            "<section class='panel' style='margin-top:14px;'>",
            "<h2>Long-running topics</h2>",
            "<table><thead><tr><th>Topic</th><th>Avg min</th><th>Chats</th></tr></thead><tbody>",
        ]
        for row in topics:
            if not isinstance(row, dict):
                continue
            out.append(
                f"<tr><td>{html.escape(str(row.get('topic') or ''))}</td>"
                f"<td>{float(row.get('avg_minutes') or 0):.1f}</td>"
                f"<td>{_fmt_int(row.get('conversations'))}</td></tr>"
            )
        out.append("</tbody></table></section>")
        return "\n".join(out)

    def _search_body(self, qs: dict[str, list[str]]) -> str:
        q = (qs.get("q") or [""])[0].strip()
        topic = (qs.get("topic") or [""])[0].strip()
        sort = (qs.get("sort") or ["latest"])[0].strip()
        range_key = (qs.get("range") or ["all"])[0].strip()
        grouped = (qs.get("grouped") or ["1"])[0].strip() != "0"
        limit = self._parse_int(qs, "limit", default=50, min_value=1, max_value=500)
        form = f"""
          <section class="panel">
            <form method="GET" action="/search" class="search-form">
              <input type="text" name="q" value="{html.escape(q)}" placeholder='FTS query, e.g. "book AND writing"' />
              <select name="range">
                {self._option('all', 'All time', range_key)}
                {self._option('year', 'Year', range_key)}
                {self._option('quarter', 'Quarter', range_key)}
                {self._option('month', 'Month', range_key)}
              </select>
              <select name="sort">
                {self._option('latest', 'Latest', sort)}
                {self._option('messages', 'Most messages', sort)}
                {self._option('oldest', 'Oldest', sort)}
                {self._option('title', 'Title', sort)}
              </select>
              <button class="primary" type="submit">Search</button>
              <input type="hidden" name="topic" value="{html.escape(topic)}" />
            </form>
            <div class="toolbar">
              <label><input type="checkbox" name="grouped" value="1" {"checked" if grouped else ""} form="unused" /> Grouped by conversation</label>
              <span class="hint">Topic filter: {html.escape(topic or "none")}. Uses SQLite FTS5 for text search.</span>
            </div>
          </section>
        """
        if q:
            try:
                results = self._search_html(q, limit=limit, grouped=grouped)
            except Exception as e:
                results = f"<section class='panel'><pre>Error: {html.escape(str(e))}</pre></section>"
            return form + results
        if topic:
            data = self._conversations_data(qs)
            return form + self._conversation_table_html(data)
        return form + "<section class='empty'>Enter a query or open a topic from the dashboard.</section>"

    @staticmethod
    def _option(value: str, label: str, current: str) -> str:
        selected = " selected" if current == value else ""
        return f"<option value='{html.escape(value)}'{selected}>{html.escape(label)}</option>"

    def _conversation_table_html(self, data: dict[str, object]) -> str:
        rows = data.get("items") if isinstance(data.get("items"), list) else []
        if not rows:
            return "<section class='empty'>No conversations match this filter.</section>"
        out = [
            "<section class='panel' style='margin-top:14px;'>",
            "<div class='section-head'><div><h2>Conversations</h2><div class='subtle'>Open any row to inspect the transcript.</div></div></div>",
            "<table><thead><tr><th>Title</th><th>Created</th><th>Messages</th><th>Hits</th></tr></thead><tbody>",
        ]
        q = str(data.get("query") or "")
        for row in rows:
            if not isinstance(row, dict):
                continue
            href_params = {"conversation_id": str(row.get("conversation_id") or "")}
            if q:
                href_params["q"] = q
            href = "/conv?" + urlencode(href_params)
            out.append(
                f"<tr><td><a href='{href}'><strong>{html.escape(str(row.get('title') or 'Untitled'))}</strong></a>"
                f"<div class='meta'>{html.escape(str(row.get('conversation_id') or ''))}</div></td>"
                f"<td>{html.escape(str(row.get('create_time_iso') or ''))}</td>"
                f"<td>{_fmt_int(row.get('message_count'))}</td>"
                f"<td>{_fmt_int(row.get('hits')) if row.get('hits') is not None else ''}</td></tr>"
            )
        out.append("</tbody></table></section>")
        return "\n".join(out)

    def _conversation_body(self, qs: dict[str, list[str]]) -> str:
        conversation_id = (qs.get("conversation_id") or [""])[0].strip()
        q = (qs.get("q") or [""])[0].strip()

        if not conversation_id:
            return "<p>Missing <code>conversation_id</code>. Go back to <a href='/search'>search</a>.</p>"

        highlight_terms = self._extract_highlight_terms(q)
        conn = _connect(self.db_path)
        cur = conn.cursor()

        conv = cur.execute(
            """
            SELECT conversation_id, title, create_time_iso, update_time_iso
            FROM conversations
            WHERE conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()
        if not conv:
            return f"<p>Unknown conversation: <code>{html.escape(conversation_id)}</code>. Go back to <a href='/search'>search</a>.</p>"

        msgs = cur.execute(
            """
            SELECT id, seq, role, create_time_iso, text
            FROM messages
            WHERE conversation_id = ?
            ORDER BY seq
            """,
            (conversation_id,),
        ).fetchall()

        back_href = f"/search?{urlencode({'q': q})}" if q else "/search"
        header = (
            f"<p><a href='{back_href}'>&larr; Back to search</a></p>"
            f"<section class='panel'><h2>{html.escape(conv['title'] or '')}</h2>"
            f"<div class='meta'>conv=<code>{html.escape(conversation_id)}</code></div>"
            f"<div class='meta'>created={html.escape(conv['create_time_iso'] or '')} &middot; updated={html.escape(conv['update_time_iso'] or '')}</div></section>"
        )

        out: list[str] = [header]
        if q:
            out.append(
                f"<div class='hint'>Highlighting terms from query: <code>{html.escape(', '.join(highlight_terms))}</code></div>"
            )

        related_html = self._related_conversations_html(cur, conversation_id)
        if related_html:
            out.append(related_html)

        if not msgs:
            out.append("<p>No messages.</p>")
            return "\n".join(out)

        out.append("<h2 style='margin:16px 0 8px;'>Transcript</h2>")
        for m in msgs:
            text = (m["text"] or "").strip()
            snippet = text.splitlines()[0][:140] if text else ""
            is_open = False
            if highlight_terms and text:
                lower = text.lower()
                is_open = any(t.lower() in lower for t in highlight_terms)
            summary = (
                f"[{m['seq']}] {html.escape(m['role'] or '')} "
                f"{html.escape(m['create_time_iso'] or '')} "
                f"<span class='meta'>{html.escape(snippet)}</span>"
            ).strip()
            out.append(f"<details id='m{m['id']}' {'open' if is_open else ''}>")
            out.append(f"<summary>{summary}</summary>")
            out.append(f"<pre>{self._highlight(text, highlight_terms)}</pre>")
            out.append("</details>")
        return "\n".join(out)

    @staticmethod
    def _related_conversations_html(cur: sqlite3.Cursor, conversation_id: str) -> str:
        has_terms = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_terms' LIMIT 1"
        ).fetchone()
        if not has_terms:
            return ""

        rows = cur.execute(
            """
            SELECT c.conversation_id, c.title, rel.score
            FROM (
              SELECT t2.conversation_id AS conversation_id,
                     SUM(t1.weight * t2.weight) AS score
              FROM conversation_terms t1
              JOIN conversation_terms t2 ON t1.term = t2.term
              WHERE t1.conversation_id = ?
                AND t2.conversation_id != ?
              GROUP BY t2.conversation_id
              ORDER BY score DESC
              LIMIT 15
            ) rel
            JOIN conversations c ON c.conversation_id = rel.conversation_id
            ORDER BY rel.score DESC
            """,
            (conversation_id, conversation_id),
        ).fetchall()
        if not rows:
            return ""

        out = ["<section class='panel' style='margin-top:14px;'><h2>Related conversations</h2>"]
        out.append(
            "<div class='hint'>Based on the offline TF-IDF term overlap index.</div>"
        )
        out.append("<ul>")
        for r in rows:
            href = f"/conv?conversation_id={html.escape(r['conversation_id'])}"
            out.append(
                f"<li><a href='{href}'>{html.escape(r['title'] or '')}</a> <span class='meta'>(score={r['score']:.3f})</span></li>"
            )
        out.append("</ul></section>")
        return "\n".join(out)

    @staticmethod
    def _parse_int(
        qs: dict[str, list[str]],
        key: str,
        *,
        default: int,
        min_value: int,
        max_value: int,
    ) -> int:
        raw = (qs.get(key) or [str(default)])[0].strip()
        try:
            value = int(raw)
        except Exception:
            return default
        return max(min_value, min(max_value, value))

    @staticmethod
    def _extract_highlight_terms(query: str) -> list[str]:
        if not query:
            return []
        raw = re.findall(r"[A-Za-z0-9_][A-Za-z0-9_'-]{2,}", query)
        drop = {"and", "or", "not", "near"}
        out: list[str] = []
        seen = set()
        for t in raw:
            tl = t.lower()
            if tl in drop:
                continue
            if tl in seen:
                continue
            seen.add(tl)
            out.append(t)
        return out[:20]

    @staticmethod
    def _highlight(text: str, terms: list[str]) -> str:
        if not text:
            return ""
        escaped = html.escape(text)
        if not terms:
            return escaped
        safe_terms = [t for t in terms if t and len(t) >= 3]
        if not safe_terms:
            return escaped
        pattern = re.compile(
            "(" + "|".join(re.escape(t) for t in sorted(safe_terms, key=len, reverse=True)) + ")",
            re.IGNORECASE,
        )
        return pattern.sub(r"<mark>\1</mark>", escaped)

    def _search_html(self, query: str, *, limit: int, grouped: bool) -> str:
        conn = _connect(self.db_path)
        cur = conn.cursor()
        rows = cur.execute(
            """
            SELECT
              m.id,
              m.conversation_id,
              c.title AS title,
              m.seq,
              m.role,
              m.create_time_iso,
              m.text,
              bm25(messages_fts) AS score
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN conversations c ON c.conversation_id = m.conversation_id
            WHERE messages_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()

        if not rows:
            return "<section class='empty'>No matches.</section>"

        highlight_terms = self._extract_highlight_terms(query)

        if not grouped:
            out = ["<section class='panel' style='margin-top:14px;'><h2>Results</h2>"]
            for r in rows:
                conv_link = (
                    f"/conv?conversation_id={html.escape(r['conversation_id'])}&q={html.escape(query)}#m{r['id']}"
                )
                out.append("<div class='topic-row' style='display:block;'>")
                out.append(
                    f"<div class='meta'>score={r['score']:.3f} &middot; role={html.escape(r['role'] or '')} &middot; time={html.escape(r['create_time_iso'] or '')} &middot; <a href='{conv_link}'>open</a></div>"
                )
                out.append(
                    f"<div class='meta'>conv=<code>{html.escape(r['conversation_id'])}</code> &middot; seq={r['seq']}</div>"
                )
                out.append(f"<div><strong>{html.escape(r['title'] or '')}</strong></div>")
                out.append(f"<pre>{self._highlight((r['text'] or '').strip(), highlight_terms)}</pre>")
                out.append("</div>")
            out.append("</section>")
            return "\n".join(out)

        groups: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            groups.setdefault(r["conversation_id"], []).append(r)

        ordered = sorted(
            groups.items(),
            key=lambda kv: (min(x["score"] for x in kv[1]), -(len(kv[1]))),
        )

        out = ["<section class='panel' style='margin-top:14px;'><h2>Results grouped by conversation</h2>"]
        for conversation_id, hits in ordered:
            best = min(h["score"] for h in hits)
            title = hits[0]["title"] or ""
            conv_href = f"/conv?conversation_id={html.escape(conversation_id)}&q={html.escape(query)}"
            out.append("<div class='topic-row' style='display:block;'>")
            out.append(
                f"<div><strong><a href='{conv_href}'>{html.escape(title)}</a></strong></div>"
            )
            out.append(
                f"<div class='meta'>conv=<code>{html.escape(conversation_id)}</code> &middot; hits={len(hits)} &middot; best_score={best:.3f}</div>"
            )

            out.append("<div style='margin-top:10px;'>")
            for h in hits[:5]:
                msg_href = f"/conv?conversation_id={html.escape(conversation_id)}&q={html.escape(query)}#m{h['id']}"
                snippet = ((h["text"] or "").strip()[:360]).strip()
                out.append(
                    "<div style='margin: 8px 0;'>"
                    f"<div class='meta'>score={h['score']:.3f} &middot; seq={h['seq']} &middot; role={html.escape(h['role'] or '')} &middot; <a href='{msg_href}'>jump to message</a></div>"
                    f"<pre>{self._highlight(snippet, highlight_terms)}</pre>"
                    "</div>"
                )
            if len(hits) > 5:
                out.append(
                    f"<div class='meta'>{len(hits) - 5} more hits in this conversation.</div>"
                )
            out.append("</div>")
            out.append("</div>")
        out.append("</section>")
        return "\n".join(out)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Serve a local web UI for the ChatGPT export SQLite index.")
    p.add_argument("--db", default=default_db_path(), help="SQLite DB path")
    p.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8765, help="Port to bind (default: 8765)")
    args = p.parse_args(argv)

    Handler.db_path = Path(args.db)
    server = HTTPServer((args.host, args.port), Handler)
    print(f"serving on http://{args.host}:{args.port} (db={Handler.db_path})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
