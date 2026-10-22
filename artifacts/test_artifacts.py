"""Stdlib-only publisher and shared-deployment regression check."""
import importlib.util
import json
from pathlib import Path
import tempfile

spec = importlib.util.spec_from_file_location('artifacts_plugin', Path(__file__).with_name('__init__.py'))
assert spec and spec.loader
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


def check():
    mapping = json.loads(Path(__file__).parents[1].joinpath('profile-plugins.json').read_text())
    entries = [e for e in mapping['mappings'] if e['plugin'] == 'artifacts']
    assert {e['profile'] for e in entries} == {'default', 'coding'}
    assert all(e['source'] == 'artifacts' and e['version'] == '1.0.0' for e in entries)
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        plugin.ROOT = base / 'published'
        source = base / 'source'
        source.mkdir()
        (source / 'index.html').write_text('<h1>Shared HTML</h1>')
        (source / 'note.md').write_text('# Shared Markdown')
        for slug, entry in [('html', 'index.html'), ('markdown', 'note.md')]:
            result = json.loads(plugin.publish({'source': str(source), 'slug': slug, 'title': slug, 'entry': entry}))
            assert result['ok'], result
            assert result['url'] == f'/artifacts/{slug}/{entry}'
            assert (plugin.ROOT / slug / entry).read_bytes() == (source / entry).read_bytes()
            assert json.loads((plugin.ROOT / slug / 'artifact.json').read_text())['entry'] == entry
        assert not json.loads(plugin.publish({'source': str(source), 'slug': 'html', 'title': 'duplicate'}))['ok']
        for slug in ['../escape', '', 'UPPER']:
            assert not json.loads(plugin.publish({'source': str(source), 'slug': slug, 'title': 'bad'}))['ok']
        (source / 'link').symlink_to(source / 'note.md')
        assert not json.loads(plugin.publish({'source': str(source), 'slug': 'bad-link', 'title': 'bad'}))['ok']
        assert not list(plugin.ROOT.glob('.publish-*'))
    print('PASS: shared profile mappings, HTML/Markdown publishing, metadata, duplicates, invalid slugs and symlinks')


if __name__ == '__main__':
    check()
