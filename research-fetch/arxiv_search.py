"""arXiv search tool."""

import datetime
import shlex

from .common import CLI, run as execute, text

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
SCHEMA = {
    'name': 'arxiv_search',
    'description': 'Search arXiv with optional title, author, category and date filters; return Markdown. No result persistence.',
    'parameters': {'type': 'object', 'properties': ARXIV_PROPERTIES, 'required': [], 'additionalProperties': False},
}


def command(args):
    if not isinstance(args, dict):
        raise ValueError('arguments must be an object')
    allowed = set(ARXIV_PROPERTIES)
    if set(args) - allowed:
        raise ValueError('unknown arguments: ' + ', '.join(sorted(set(args) - allowed)))
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


def run(ctx, args, **kwargs):
    return execute(ctx, args, command)


def register(ctx):
    ctx.register_tool(name='arxiv_search', toolset='research-fetch', schema=SCHEMA,
                      handler=lambda args, **kwargs: run(ctx, args, **kwargs))
