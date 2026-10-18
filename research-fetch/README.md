# research-fetch

Native Hermes plugin with two tools: youtube_transcript and arxiv_search.

Bundled skills: 0. The plugin exposes two existing, profile-resident replacement skills when their files exist under the active Hermes home (resolved by the public hermes_constants.get_hermes_home() helper, honoring HERMES_HOME):

- research-fetch:youtube-transcript → skills/media/youtube-transcript/SKILL.md (replaces the old global youtube-content reference).
- research-fetch:arxiv-research → skills/research/arxiv-research/SKILL.md (replaces the old global arxiv reference).

These are read-only namespaced references through ctx.register_skill, not bundled copies. Each missing file is skipped independently; both tools still register in profiles without either skill. No skill files are created, copied, symlinked, or modified, and legacy global skills and X tools/skills are untouched. Bare skill names remain profile-owned. Load replacements by their full names with skill_view.

Only the public register(ctx), ctx.register_tool, ctx.register_skill, ctx.dispatch_tool and get_hermes_home APIs are used. Handlers dispatch a fixed python3 /workspace/research-cli/research_cli.py command through Hermes terminal (90 second timeout); they do not execute host subprocesses or make network requests. User values are validated and shlex-quoted, options use = and positional values follow --. CLI handles external URL validation and retrieval. The target profile must already configure terminal isolation and have the CLI mounted at that path.

YouTube uses url-fetch --subtitles, preserving the original-track default unless sub_langs is specified. CLI handles raw JSON internally and renders timestamped Markdown. arXiv uses arxiv-search. Both set --no-save: no plugin state/cache/context or persistent results. CLI output is capped at 12000 characters; truncation and subtitle-download failures are retained verbatim, not reported as complete transcripts. Responses are JSON strings containing markdown or error.

Tests: python3 -m unittest discover -s research-fetch -p 'test_*.py' -v

Enable using the standard Hermes plugins CLI in the default profile; new processes/sessions load the schemas, existing sessions may retain cached schemas. No gateway restart required by this installation.
