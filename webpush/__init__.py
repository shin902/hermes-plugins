"""Mirror Hermes cron notifications to the Home PWA over Web Push.

Hooks are the only public seam that observes a cron run without rewriting job
records: ``post_llm_call`` carries the agent's reply, ``on_session_end`` carries
the outcome. ``ctx.current_cron_execution()`` tells both apart from interactive
chat, so this plugin never notifies about conversations.

Delivery reuses the site's ``homepage/push_store.py``: one subscription table
and one Node sender for both the site server and every profile's cron runs.
A missing checkout or a failed send is logged and never breaks the run.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import re
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

SILENT_MARKERS = ('[SILENT]', '(empty)')
MAX_BODY = 220
MAX_TITLE = 60

_STORE = None
_CLAIMS = set()
_LOCK = threading.Lock()


def _sites_home() -> Path:
    return Path(os.environ.get('WEBPUSH_SITES', '~/.hermes/sites')).expanduser()


def _load_store():
    """Import the site's push module by path; None when the checkout is absent."""
    global _STORE
    if _STORE is not None:
        return _STORE
    target = _sites_home() / 'homepage' / 'push_store.py'
    if not target.is_file():
        return None
    spec = importlib.util.spec_from_file_location('sites_push_store', target)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as error:  # noqa: BLE001 - a broken checkout must not break cron
        logger.warning('webpush: cannot load %s: %s', target, error)
        return None
    _STORE = module
    return module


def _current_run(ctx):
    try:
        return ctx.current_cron_execution()
    except Exception:  # noqa: BLE001 - older builds without the seam
        return None


def _claim(run, kind: str) -> bool:
    """One notification per (run, kind): extra turns of the same run stay quiet."""
    key = (getattr(run, 'job_id', ''), getattr(run, 'execution_id', ''), kind)
    with _LOCK:
        if key in _CLAIMS:
            return False
        _CLAIMS.add(key)
        if len(_CLAIMS) > 200:
            _CLAIMS.clear()
            _CLAIMS.add(key)
        return True


def _title(run) -> str:
    name = str(getattr(run, 'job_name', '') or getattr(run, 'job_id', '') or 'cron')
    return name.strip()[:MAX_TITLE]


def _clean(text) -> str:
    """Plain text for a lock screen: markdown syntax and code fences removed."""
    value = str(text or '')
    if '```' in value:
        value = re.sub(r'```.*?```', ' ', value, flags=re.S)
    value = re.sub(r'!\[[^\]]*\]\([^)]*\)', ' ', value)
    value = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', value)
    value = re.sub(r'[#*_>`~]+', ' ', value)
    value = re.sub(r'\s+([:：])', r'\1', value)
    value = re.sub(r'\s+', ' ', value).strip()
    return value[:MAX_BODY]


def _send(run, body: str, kind: str) -> None:
    store = _load_store()
    if store is None:
        logger.warning('webpush: push store unavailable; notification dropped')
        return
    tag = f"cron-{getattr(run, 'job_id', '')}-{getattr(run, 'started_at', '')}"
    try:
        result = store.notify(_title(run), body, '/', tag)
        logger.info('webpush: %s %s -> %s', kind, tag, result)
    except Exception as error:  # noqa: BLE001 - delivery must never break the run
        logger.warning('webpush: send failed for %s: %s', tag, error)


def register(ctx):
    def on_post_llm_call(*, assistant_response=None, **_kwargs):
        run = _current_run(ctx)
        if run is None:
            return
        text = _clean(assistant_response)
        if len(text) < 2 or any(marker in text for marker in SILENT_MARKERS):
            return
        if _claim(run, 'reply'):
            _send(run, text, 'reply')

    def on_session_end(*, failed=False, interrupted=False, turn_exit_reason=None, **_kwargs):
        run = _current_run(ctx)
        if run is None or not (failed or interrupted):
            return
        if _claim(run, 'failure'):
            reason = str(turn_exit_reason or ('interrupted' if interrupted else 'failed'))
            _send(run, f'失敗しました: {reason}'[:MAX_BODY], 'failure')

    ctx.register_hook('post_llm_call', on_post_llm_call)
    ctx.register_hook('on_session_end', on_session_end)
