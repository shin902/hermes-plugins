# research-fetch

Native Hermes plugin with two tools: youtube_transcript and arxiv_search.

新規skillなし、既存skillを利用: youtube-content と arxiv。Bundled skills: 0. X tools/skills are unchanged.

Only the public register(ctx), ctx.register_tool and ctx.dispatch_tool APIs are used. Handlers dispatch a fixed python3 /workspace/research-cli/research_cli.py command through Hermes terminal (90 second timeout); they do not execute host subprocesses or make network requests. User values are validated and shlex-quoted, options use = and positional values follow --. CLI handles external URL validation and retrieval. The target profile must already configure terminal isolation and have the CLI mounted at that path.

YouTube uses url-fetch --subtitles, preserving the original-track default unless sub_langs is specified. CLI handles raw JSON internally and renders timestamped Markdown. arXiv uses arxiv-search. Both set --no-save: no plugin state/cache/context or persistent results. CLI output is capped at 12000 characters; truncation and subtitle-download failures are retained verbatim, not reported as complete transcripts. Responses are JSON strings containing markdown or error.

Tests: python3 -m unittest discover -s research-fetch -p 'test_*.py' -v

Enable using the standard Hermes plugins CLI in the default profile; new processes/sessions load the schemas, existing sessions may retain cached schemas. No gateway restart required by this installation.
