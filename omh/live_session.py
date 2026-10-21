"""Which live Hermes TUI session a HUD read is for, from the host's own records.

The host names one TUI session two ways and only one of them reaches a widget.
``state.db`` rows, and the plan-todo records keyed to match them, use the
durable session key (``20260917_132533_8da9b8``). The per-TUI active-session
file a widget reads holds the gateway TRANSPORT id (``ebe3eaaa``, uuid4
hex[:8]) whenever its session was created rather than resumed. Both questions
are answered here, from a different host surface each, because a widget cannot
know which vocabulary its own reference is in.

``state.db`` is the host's own record of which sessions exist and which one
the user is in front of. Two HUD readers need the same answer from it:

* the effective-yolo projection (``approval_bypass``), which may only read a
  ``/yolo`` flag off a session a widget is actually rendering, and
* the plan-todo session scope (``runtime_reader._todo_summary``), which must
  not project a previous session's checklist into a new one.

Both scope the question identically -- ``source='tui'``, not ended, archived,
or hidden, not a delegation child, ordered by the host's own MRU column
(``last_activity_at``) -- so the scoping lives here once instead of as two
copies of the same SQL.

An empty result means the question is UNANSWERABLE here: no ``state.db``, an
older schema, an unreadable file, or no live TUI session at all. It is never a
negative answer, and every caller must fall back to what it would have done
without this surface rather than acting on the silence.
"""
from __future__ import annotations

from . import runtime_paths

from pathlib import Path
from typing import Any

# A host restart leaves no live row behind, and nothing re-stamps a row after
# the process that owned it is gone. Six hours bounds how long the freshest
# surviving row may keep speaking for a session someone is looking at; it is
# the same bound the approval-bypass ledger uses, for the same reason.
LIVE_TUI_SESSION_FRESH_SECONDS = 6 * 3600.0
_LIVE_TUI_SESSION_ROW_LIMIT = 32
# The lease registry holds one small entry per open chat surface. The bound is
# a guard against reading something that is not that file at all, not a
# capacity estimate; a real registry is orders of magnitude under it.
_ACTIVE_SESSION_REGISTRY_MAX_BYTES = 1 << 20


def live_tui_session_rows(hermes_home: str = "") -> list[dict[str, Any]]:
    """Live TUI session rows, most recently active first.

    Each row carries the host's own ``id``, the raw ``started_at`` and
    ``activity`` stamps, and the parsed ``model_config`` dict. The stamps stay
    raw on purpose: what an unusable timestamp means differs per caller, so
    this reader does not decide it. Rows whose ``model_config`` is unparseable
    or names a delegation parent are dropped, matching the host's own view of
    which sessions a person is driving.
    """
    import json
    import sqlite3
    from urllib.parse import quote

    home = Path(hermes_home).expanduser() if hermes_home else runtime_paths.default_hermes_home()
    path = home / "state.db"
    if not path.exists():
        return []
    try:
        connection = sqlite3.connect(
            f"file:{quote(str(path))}?mode=ro", uri=True, timeout=0.2
        )
    except sqlite3.Error:
        return []
    rows: list[dict[str, Any]] = []
    try:
        cursor = connection.execute(
            """
            SELECT id, model_config, COALESCE(last_activity_at, started_at), started_at
            FROM sessions
            WHERE source = 'tui'
              AND ended_at IS NULL
              AND COALESCE(archived, 0) = 0
              AND COALESCE(hidden, 0) = 0
              AND (
                model_config IS NULL
                OR NOT json_valid(model_config)
                OR json_extract(model_config, '$._delegate_from') IS NULL
              )
            ORDER BY COALESCE(last_activity_at, started_at) DESC
            LIMIT ?
            """,
            (_LIVE_TUI_SESSION_ROW_LIMIT,),
        )
        for session_id, raw_config, activity, started_at in cursor.fetchall():
            try:
                config = json.loads(raw_config) if raw_config else {}
            except (ValueError, TypeError):
                continue
            if not isinstance(config, dict) or config.get("_delegate_from"):
                continue
            rows.append(
                {
                    "id": str(session_id or ""),
                    "model_config": config,
                    "activity": activity,
                    "started_at": started_at,
                }
            )
    except (sqlite3.Error, TypeError, ValueError):
        return []
    finally:
        try:
            connection.close()
        except sqlite3.Error:
            pass
    return rows


def session_row(hermes_home: str, session_id: str) -> dict[str, Any] | None:
    """The host's own row for ONE session, whatever surface it runs on.

    ``live_tui_session_rows`` above answers "which session is a person looking
    at", and every filter in it exists for that question. This answers a
    different one -- "does the host say THIS session is still running" -- and
    so shares none of them.

    The distinction is not academic. Routes are written from whatever surface
    the agent runs on: measured on the owner's two homes, the miku profile's
    routes come from ``slack`` (132) and ``subagent`` (33) sessions and none
    from a TUI, and the default home already has ``desktop`` ones beside its
    TUI ones. Asking the TUI list whether a slack writer is live answers
    "it is not in this list", which is not the same statement, and reading it
    as "not live" restored a baseline out from under a session mid-dispatch
    (#1737 review).

    So: no ``source`` filter, because a route's writer is usually not a TUI.
    No ``_delegate_from`` filter, because a delegated child writes routes too
    and its liveness is a real question. No ``archived``/``hidden`` filter,
    because those say where a session is displayed, not whether it is over.

    Returns ``source``, the raw ``ended_at``, and the raw ``activity`` stamp,
    or ``None`` when the question is UNANSWERABLE here: no ``state.db``, an
    older schema, an unreadable file, or no row with this id. ``None`` is
    never a statement that the session ended, and the caller must fall back
    to what it would have done without this surface.
    """
    import sqlite3
    from urllib.parse import quote

    reference = str(session_id or "")
    if not reference:
        return None
    home = Path(hermes_home).expanduser() if hermes_home else runtime_paths.default_hermes_home()
    path = home / "state.db"
    if not path.exists():
        return None
    try:
        connection = sqlite3.connect(
            f"file:{quote(str(path))}?mode=ro", uri=True, timeout=0.2
        )
    except sqlite3.Error:
        return None
    try:
        found = connection.execute(
            """
            SELECT id, source, ended_at, COALESCE(last_activity_at, started_at)
            FROM sessions
            WHERE id = ?
            LIMIT 1
            """,
            (reference,),
        ).fetchone()
    except (sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        try:
            connection.close()
        except sqlite3.Error:
            pass
    if not found:
        return None
    return {
        "id": str(found[0] or ""),
        "source": str(found[1] or ""),
        "ended_at": found[2],
        "activity": found[3],
    }


def tui_session_durable_id(hermes_home: str, transport_ref: str) -> str:
    """The durable session key the host paired with a TUI's transport id.

    ``$HERMES_HOME/runtime/active_sessions.json`` is the host's own lease
    registry of currently open chat surfaces, and the only place on disk where
    the two names for one session meet: each entry carries the durable key as
    ``session_id`` and the transport id as ``metadata.live_session_id``. The
    host mints both on one line of ``session.create`` and hands them to the
    registry together, so the pairing is the host's own statement of identity,
    not a correlation inferred here. The transport id is in no ``state.db``
    column, which is why the widget's reference resolves to nothing without
    this read.

    Only ``surface: 'tui'`` entries answer, matching the scope of
    ``live_tui_session_rows`` -- a gateway surface's lease says nothing about
    which TUI is rendering.

    An empty answer means the question is UNANSWERABLE here: no registry, an
    unreadable or unexpected one, or no lease naming this transport id. It is
    never a statement that the reference is invalid, and a caller must fall
    back to treating the reference as its own identity.

    The host claims a session's lease on its FIRST REAL TURN rather than at
    creation, deliberately, so that abandoned drafts hold no slot. A TUI
    nobody has prompted in therefore has no entry at all -- and owns no plan
    either, so the empty answer and the empty panel agree.
    """
    import json

    reference = str(transport_ref or "")
    if not reference:
        return ""
    home = Path(hermes_home).expanduser() if hermes_home else runtime_paths.default_hermes_home()
    path = home / "runtime" / "active_sessions.json"
    try:
        if path.stat().st_size > _ACTIVE_SESSION_REGISTRY_MAX_BYTES:
            return ""
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    entries = payload.get("entries") if isinstance(payload, dict) else None
    # Newest first. Entries are appended as leases are claimed, and a transport
    # id is eight hex characters, so the same string can recur across a long
    # machine history; the most recent claim is the one someone is looking at.
    for entry in reversed(entries if isinstance(entries, list) else []):
        if not isinstance(entry, dict) or entry.get("surface") != "tui":
            continue
        metadata = entry.get("metadata")
        live = metadata.get("live_session_id") if isinstance(metadata, dict) else None
        if not isinstance(live, str) or live != reference:
            continue
        durable = entry.get("session_id")
        return durable if isinstance(durable, str) else ""
    return ""
