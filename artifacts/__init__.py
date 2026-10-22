"""Publish explicitly selected static folders to the personal Tailnet site."""
import json
from pathlib import Path
import re
import shutil
import tempfile

ROOT = Path.home() / '.hermes/sites/homepage/artifacts'
BASE = '/artifacts/'


def publish(args, **kwargs):
    stage = None
    try:
        if set(args) - {'source', 'slug', 'title', 'description', 'entry'}:
            raise ValueError('Unknown argument')
        slug = args['slug']
        if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', slug):
            raise ValueError('slug must be 1–80 lowercase letters, digits or hyphens')
        source = Path(args['source']).expanduser()
        if not source.is_absolute() or source.is_symlink() or not source.is_dir():
            raise ValueError('source must be an absolute folder path, not a symlink')
        if source.resolve().is_relative_to(ROOT.resolve()) or ROOT.resolve().is_relative_to(source.resolve()):
            raise ValueError('source must not overlap the published artifacts folder')
        entry = args.get('entry', 'index.html')
        rel = Path(entry)
        if rel.is_absolute() or '..' in rel.parts or any(p.startswith('.') for p in rel.parts):
            raise ValueError('Invalid entry path')
        if rel.suffix.lower() not in {'.html', '.htm', '.md', '.markdown'} or not (source / rel).is_file():
            raise ValueError('entry must be an existing HTML or Markdown file')
        title = args['title']
        description = args.get('description', '')
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ValueError('title must contain 1–200 characters')
        if not isinstance(description, str) or len(description) > 1000:
            raise ValueError('description must be at most 1000 characters')
        # Only the explicitly selected folder is copied. Hidden files and symlinks are refused.
        for path in source.rglob('*'):
            if path.is_symlink() or any(p.startswith('.') for p in path.relative_to(source).parts):
                raise ValueError('Remove symlinks and hidden files before publishing')
            if not (path.is_file() or path.is_dir()):
                raise ValueError('Only regular files and directories may be shared')
        ROOT.mkdir(parents=True, exist_ok=True)
        if (ROOT / slug).exists():
            raise ValueError('slug already registered; existing artifacts are never overwritten')
        stage = Path(tempfile.mkdtemp(prefix='.publish-', dir=ROOT))
        shutil.copytree(source, stage, dirs_exist_ok=True)
        (stage / 'artifact.json').write_text(json.dumps({'title': title, 'description': description, 'entry': entry}, ensure_ascii=False), encoding='utf-8')
        stage.rename(ROOT / slug)
        stage = None
        return json.dumps({'ok': True, 'url': BASE + slug + '/' + entry, 'index': BASE + 'index.html'}, ensure_ascii=False)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False)
    finally:
        if stage is not None:
            shutil.rmtree(stage)


def register(ctx):
    ctx.register_tool(name='artifact_register', toolset='artifacts', handler=publish, schema={
        'name': 'artifact_register',
        'description': 'Publish a user-selected folder of HTML/Markdown and assets to the personal Tailnet artifacts site. Copies the entire folder; obtain permission to share its contents. Adds a card automatically. Existing slugs cannot be overwritten.',
        'parameters': {'type': 'object', 'additionalProperties': False,
                       'properties': {k: {'type': 'string', 'description': v} for k, v in {
                           'source': 'Absolute path of the folder to share; no hidden files or symlinks',
                           'slug': 'Unique lowercase URL slug (letters, digits, hyphens)',
                           'title': 'Card title', 'description': 'Optional card description',
                           'entry': 'Relative HTML or Markdown entry path; default index.html'}.items()},
                       'required': ['source', 'slug', 'title']}})
