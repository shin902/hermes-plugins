"""Regression check for the cron-to-push hook contract.

Run directly: python3 test_webpush.py (exit 0 = pass).
"""
import importlib.util
import sys
from pathlib import Path

MODULE = Path(__file__).with_name('__init__.py')


def load():
    spec = importlib.util.spec_from_file_location('webpush_plugin', MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules['webpush_plugin'] = module
    spec.loader.exec_module(module)
    return module


class Run:
    job_id = 'job-1'
    job_name = '監視ジョブ'
    execution_id = 'exec-1'
    started_at = '2026-10-31T09:00:00+09:00'


class Store:
    def __init__(self):
        self.sent = []

    def notify(self, title, body, url, tag):
        self.sent.append((title, body, url, tag))
        return {'sent': 1, 'failed': 0, 'pruned': 0}


class Ctx:
    def __init__(self, run):
        self.run = run
        self.hooks = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def current_cron_execution(self):
        return self.run


def main():
    module = load()
    store = Store()
    module._STORE = store

    ctx = Ctx(Run())
    module.register(ctx)
    assert set(ctx.hooks) == {'post_llm_call', 'on_session_end'}, ctx.hooks

    reply = ctx.hooks['post_llm_call']
    reply(assistant_response='## 見出し\n**結果**: 3件 更新', model='m', platform='cron')
    assert len(store.sent) == 1, store.sent
    title, body, url, tag = store.sent[0]
    assert title == '監視ジョブ', title
    assert body == '見出し 結果: 3件 更新', body
    assert url == '/' and tag.startswith('cron-job-1-'), (url, tag)

    # A second turn of the same run must not notify twice.
    reply(assistant_response='同じ実行の続き')
    assert len(store.sent) == 1, store.sent

    # Silent and empty replies stay quiet, including code-only bodies.
    reply(assistant_response='[SILENT]')
    reply(assistant_response='(empty)')
    reply(assistant_response='   ')
    assert len(store.sent) == 1, store.sent

    # Failures notify once, and reuse the same tag so the device replaces the reply.
    end = ctx.hooks['on_session_end']
    end(failed=True, interrupted=False, turn_exit_reason='tool_error')
    end(failed=True, interrupted=False, turn_exit_reason='tool_error')
    assert len(store.sent) == 2, store.sent
    assert store.sent[1][1] == '失敗しました: tool_error', store.sent[1]
    assert store.sent[1][3] == store.sent[0][3]

    # Interactive chat (no cron identity) never pushes.
    quiet = Ctx(None)
    module.register(quiet)
    quiet.hooks['post_llm_call'](assistant_response='普通の会話')
    quiet.hooks['on_session_end'](failed=True)
    assert len(store.sent) == 2, store.sent

    # Long replies are truncated for a lock screen, newlines collapsed.
    module._CLAIMS.clear()
    module.register(ctx)
    ctx.hooks['post_llm_call'](assistant_response='あ' * 900)
    assert len(store.sent[2][1]) == module.MAX_BODY, len(store.sent[2][1])

    print('webpush plugin: all checks passed')


if __name__ == '__main__':
    main()
