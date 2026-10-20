"""RSS monitor: single fetch, durable item-ID dedupe, compact new-item batches."""

from __future__ import annotations

import fcntl
import hashlib
import http.client
import html
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"
MAX_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_CHARS = 6800
EMPTY_OUTPUT = '{"items":[]}'
TRANSIENT_STATUSES = {404, 408, 429, 500, 502, 503, 504}


class TransientFetchError(RuntimeError):
    """A failed single fetch eligible for quiet carry-over to the next scheduled tick."""


def clean_text(value: str, limit: int) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", value))).strip()[:limit]


def parse_feed(body: bytes | str) -> list[dict[str, str]]:
    root = ET.fromstring(body)
    if root.tag == ATOM + "feed":
        entries = root.findall(ATOM + "entry")
        items = []
        for entry in entries:
            link = next((e.get("href", "") for e in entry.findall(ATOM + "link")
                         if e.get("rel", "alternate") == "alternate"), "")
            items.append({
                "id": entry.findtext(ATOM + "id", ""),
                "title": clean_text(entry.findtext(ATOM + "title", ""), 160),
                "url": link,
                "published": entry.findtext(ATOM + "published", ""),
                "description": clean_text(entry.findtext(MEDIA + "group/" + MEDIA + "description", ""), 320),
            })
    elif root.tag == "rss":
        items = [{
            "id": e.findtext("guid") or e.findtext("link", ""),
            "title": clean_text(e.findtext("title", ""), 160),
            "url": e.findtext("link", ""),
            "published": e.findtext("pubDate", ""),
            "description": clean_text(e.findtext("description", ""), 320),
        } for e in root.findall("channel/item")]
    else:
        raise ValueError("RSS/Atomではない応答を受信しました")
    if any(not item["id"] or not item["url"] for item in items):
        raise ValueError("記事IDまたはURLのないフィードです")
    return list({item["id"]: item for item in items}.values())


def youtube_channel(url: str) -> str | None:
    """Channel identity validation, not an alternative fetch route."""
    parsed = urllib.parse.urlsplit(url)
    channel = urllib.parse.parse_qs(parsed.query).get("channel_id", [""])[0]
    if (parsed.scheme == "https" and parsed.hostname in {"www.youtube.com", "youtube.com"}
            and parsed.path == "/feeds/videos.xml"
            and re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel)):
        return channel
    return None


def record_fetch(path: Path | None, record: dict) -> None:
    """Bounded, credential-free diagnostics independent of the monitor's stable stdout."""
    if path is None:
        return
    record = dict(record)
    for key in ('source_url', 'request_url', 'final_url'):
        if key in record:
            parts = urllib.parse.urlsplit(record[key])
            host = parts.hostname or ''
            query = urllib.parse.urlencode([(key, value if key in {'channel_id', 'playlist_id'} else '[redacted]')
                    for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)])
            record[key] = urllib.parse.urlunsplit((parts.scheme, host, parts.path, query, ''))
    record.pop('body_excerpt', None)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if path.exists() and path.stat().st_size > 2 * MAX_BYTES:
                os.replace(path, path.with_suffix(".jsonl.1"))
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"at": time.time(), "pid": os.getpid(),
                                         "python": sys.version.split()[0], **record},
                                        ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"RSS診断ログ保存失敗: {exc}", file=sys.stderr)


def response_details(response, body: bytes) -> dict:
    headers = getattr(response, "headers", {})
    return {"status": getattr(response, "code", 200),
            "final_url": response.geturl() if hasattr(response, "geturl") else "",
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "headers": {key: headers[key] for key in
                        ("Content-Type", "Date", "Expires", "Cache-Control", "Age", "Server", "Via", "X-Cache")
                        if key in headers}}


def fetch_feed(url: str, *, diagnostics_path: Path | None = None) -> list[dict[str, str]]:
    """One request to the configured URL; no retry, fallback or cache-buster."""
    started = time.monotonic()
    diagnostic = {"source_url": url, "request_url": url, "attempt": 1}
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "hermes-cron-monitor"})
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read(MAX_BYTES + 1)
            diagnostic.update(response_details(response, body))
        if len(body) > MAX_BYTES:
            raise ValueError("RSSが取得上限の2MiBを超えました")
        items = parse_feed(body)
        channel = youtube_channel(url)
        if channel:
            root = ET.fromstring(body)
            if root.findtext("{http://www.youtube.com/xml/schemas/2015}channelId") not in {channel, channel[2:]}:
                raise ValueError("YouTube応答のchannelIdが監視対象と一致しません")
        diagnostic.update(outcome="ok", item_count=len(items), elapsed=time.monotonic() - started)
        record_fetch(diagnostics_path, diagnostic)
        return items
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(4096)
            diagnostic.update(response_details(exc, body))
            diagnostic["body_excerpt"] = clean_text(body.decode("utf-8", errors="replace"), 1000)
        finally:
            exc.close()
        error = f"HTTP {exc.code}"
        diagnostic.update(outcome="error", error=error, elapsed=time.monotonic() - started)
        record_fetch(diagnostics_path, diagnostic)
        if exc.code in TRANSIENT_STATUSES:
            raise TransientFetchError(error) from exc
        raise
    except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead, http.client.RemoteDisconnected) as exc:
        diagnostic.update(outcome="error", error=type(exc).__name__, elapsed=time.monotonic() - started)
        record_fetch(diagnostics_path, diagnostic)
        raise TransientFetchError(str(exc)) from exc
    except (ValueError, ET.ParseError) as exc:
        diagnostic.update(outcome="error", error=type(exc).__name__, elapsed=time.monotonic() - started)
        record_fetch(diagnostics_path, diagnostic)
        raise


def initial_state(url: str, known_ids: list[str]) -> dict:
    return {"version": 2, "url": url, "seen_ids": sorted(set(known_ids)),
            "pending": [], "output": EMPTY_OUTPUT}


def advance(state: dict, items: list[dict[str, str]], *, acknowledged: bool = True) -> dict:
    seen = set(state["seen_ids"])
    new_items = [item for item in items if item["id"] not in seen]
    pending = list(state["pending"]) + list(reversed(new_items))
    if not acknowledged:
        return {**state, "seen_ids": sorted(seen | {item["id"] for item in items}),
                "pending": pending}
    batch = []
    while pending and len(batch) < 6:
        candidate = json.dumps({"items": batch + [pending[0]]}, ensure_ascii=False, separators=(",", ":"))
        if len(candidate) > MAX_OUTPUT_CHARS:
            if not batch:
                raise ValueError("1件の記事が要約コンテキストの上限を超えました")
            break
        batch.append(pending.pop(0))
    output = (json.dumps({"items": batch}, ensure_ascii=False, separators=(",", ":"))
              if batch else state["output"])
    return {**state, "seen_ids": sorted(seen | {item["id"] for item in items}),
            "pending": pending, "output": output}


def save_state(path: Path, state: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def batch_acknowledged(state: dict, receipt: Path) -> bool:
    if state["output"] == EMPTY_OUTPUT:
        return True
    if not receipt.exists():
        return False
    try:
        recorded = json.loads(receipt.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    if not isinstance(recorded, dict):
        return False
    output = recorded.get("output")
    if (recorded.get("status") != "delivered" or not recorded.get("execution_id")
            or not isinstance(output, str)
            or recorded.get("hash") != hashlib.sha256(output.encode("utf-8")).hexdigest()):
        return False
    try:
        delivered = json.loads(output)
        expected = json.loads(state["output"])
        return ([item["id"] for item in delivered.get("items", [])]
                == [item["id"] for item in expected["items"]])
    except (json.JSONDecodeError, KeyError, TypeError):
        return False
