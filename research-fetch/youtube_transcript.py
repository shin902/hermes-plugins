"""YouTube transcript tool."""

import shlex
from urllib.parse import urlsplit

from .common import CLI, run as execute, text

PROPERTIES = {
    'url': {'type': 'string', 'description': 'Public YouTube video URL.'},
    'sub_langs': {'type': 'string', 'description': 'Optional yt-dlp subtitle language selector; omitted uses original track.'},
}
SCHEMA = {
    'name': 'youtube_transcript',
    'description': 'Fetch YouTube subtitles as timestamped Markdown via sandboxed research CLI. Default: original track. No result persistence.',
    'parameters': {'type': 'object', 'properties': PROPERTIES, 'required': ['url'], 'additionalProperties': False},
}


def command(args):
    if not isinstance(args, dict):
        raise ValueError('arguments must be an object')
    allowed = {'url', 'sub_langs'}
    if set(args) - allowed:
        raise ValueError('unknown arguments: ' + ', '.join(sorted(set(args) - allowed)))
    url = text(args.get('url'), 'url')
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or p.hostname not in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be') or p.username or p.password:
        raise ValueError('url must be a public YouTube HTTP(S) URL')
    argv = ['python3', CLI, 'url-fetch', '--subtitles', '--no-save']
    if 'sub_langs' in args:
        argv += ['--sub-langs=' + text(args['sub_langs'], 'sub_langs')]
    argv += ['--', url]
    return shlex.join(argv)


def run(ctx, args, **kwargs):
    return execute(ctx, args, command)


def register(ctx):
    ctx.register_tool(name='youtube_transcript', toolset='research-fetch', schema=SCHEMA,
                      handler=lambda args, **kwargs: run(ctx, args, **kwargs))
