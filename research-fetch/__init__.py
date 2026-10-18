"""Thin research CLI adapters; all execution uses the public terminal dispatcher."""
import datetime
import json
import shlex
from hermes_constants import get_hermes_home
from urllib.parse import urlsplit

CLI = '/workspace/research-cli/research_cli.py'
TIMEOUT = 90


def text(value, key):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000 or '\x00' in value:
        raise ValueError(f'{key} must be a nonempty string (max 4000 characters, no NUL)')
    return value


def command(tool, args):
    if not isinstance(args, dict):
        raise ValueError('arguments must be an object')
    allowed = {'url', 'sub_langs'} if tool == 'youtube_transcript' else set(ARXIV_PROPERTIES)
    if set(args) - allowed:
        raise ValueError('unknown arguments: ' + ', '.join(sorted(set(args) - allowed)))
    if tool == 'youtube_transcript':
        url = text(args.get('url'), 'url')
        p = urlsplit(url)
        if p.scheme not in ('http', 'https') or p.hostname not in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be') or p.username or p.password:
            raise ValueError('url must be a public YouTube HTTP(S) URL')
        argv = ['python3', CLI, 'url-fetch', '--subtitles', '--no-save']
        if 'sub_langs' in args:
            argv += ['--sub-langs=' + text(args['sub_langs'], 'sub_langs')]
        argv += ['--', url]
    else:
        argv = ['python3', CLI, 'arxiv-search', '--no-save']
        limit = args.get('limit', 5)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('limit must be an integer from 1 to 50')
        argv += ['--limit', str(limit)]
        for key in ARXIV_PROPERTIES:
            if key not in args or key in ('query', 'limit'):
                continue
            value = text(args[key], key)
            if key in ('date_from', 'date_to'):
                if datetime.date.fromisoformat(value).isoformat() != value:
                    raise ValueError('dates must use YYYY-MM-DD')
            if key in ('sort', 'order') and value not in ARXIV_PROPERTIES[key]['enum']:
                raise ValueError('invalid ' + key)
            argv += ['--' + key.replace('_', '-') + '=' + value]
        if args.get('date_from') and args.get('date_to') and args['date_from'] > args['date_to']:
            raise ValueError('date_from must not be after date_to')
        if 'query' in args:
            argv += ['--', text(args['query'], 'query')]
    return shlex.join(argv)


def run(ctx, tool, args, **kwargs):
    try:
        result = ctx.dispatch_tool('terminal', {'command': command(tool, args), 'timeout': TIMEOUT})
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict):
            raise ValueError('terminal returned a non-object response')
        if result.get('error') or result.get('exit_code') != 0:
            return json.dumps({'error': result.get('error') or result.get('output') or 'terminal failed', 'exit_code': result.get('exit_code')}, ensure_ascii=False)
        output = result.get('output')
        if not isinstance(output, str):
            raise ValueError('terminal response missing output')
        return json.dumps({'markdown': output}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({'error': str(exc)}, ensure_ascii=False)


ARXIV_PROPERTIES = {k: {'type': 'string', 'description': d} for k, d in {
    'query': 'Natural-language search terms (optional when using filters).',
    'title': 'Title words.', 'author': 'Author name.', 'abstract': 'Abstract words.',
    'category': 'arXiv category, e.g. cs.AI.',
    'date_from': 'Earliest submission date, YYYY-MM-DD.',
    'date_to': 'Latest submission date, YYYY-MM-DD.'}.items()}
ARXIV_PROPERTIES.update({
    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 5},
    'sort': {'type': 'string', 'enum': ['relevance', 'submittedDate', 'lastUpdatedDate']},
    'order': {'type': 'string', 'enum': ['ascending', 'descending']}})


def register(ctx):
    definitions = [
        ('youtube_transcript', 'Fetch YouTube subtitles as timestamped Markdown via sandboxed research CLI. Default: original track. No result persistence.',
         {'url': {'type': 'string', 'description': 'Public YouTube video URL.'}, 'sub_langs': {'type': 'string', 'description': 'Optional yt-dlp subtitle language selector; omitted uses original track.'}}, ['url']),
        ('arxiv_search', 'Search arXiv with optional title, author, category and date filters; return Markdown. No result persistence.', ARXIV_PROPERTIES, [])]
    for name, description, properties, required in definitions:
        def handler(args, _name=name, **kwargs):
            return run(ctx, _name, args, **kwargs)
        ctx.register_tool(name=name, toolset='research-fetch', schema={
            'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}, handler=handler)
    skills_dir = get_hermes_home() / 'skills'
    for name, relative_path in (
        ('youtube-transcript', 'media/youtube-transcript/SKILL.md'),
        ('arxiv-research', 'research/arxiv-research/SKILL.md'),
    ):
        skill_md = skills_dir / relative_path
        if skill_md.is_file():
            ctx.register_skill(name, skill_md)
