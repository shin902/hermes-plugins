#!/usr/bin/env python3
"""Offline explicit plugin deployment; dry-run by default. No config mutations."""
import argparse
import json
import os
from pathlib import Path
import re

MANUAL = {'hindsight', 'homeassistant', 'memory-wiki', 'talk-desktop', 'spotify'}


def safe_parts(value):
    if not isinstance(value, str):
        raise ValueError('Invalid identifier')
    p = Path(value)
    if p.is_absolute() or not p.parts or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', x) for x in p.parts):
        raise ValueError('Unsafe identifier')
    return p


def manifest_version(source):
    text = (source / 'plugin.yaml').read_text(encoding='utf-8')
    match = re.search(r'^version:\s*[\"\']?([^\s\"\']+)', text, re.M)
    if not match:
        raise ValueError('Missing version')
    return match.group(1)


def reconcile(repo, home, mapping, apply=False):
    repo, home = repo.resolve(), home.absolute()
    if mapping.get('schema_version') != 2 or not isinstance(mapping.get('mappings'), list):
        raise ValueError('Unsupported manifest')
    plans, seen = [], set()
    for entry in mapping['mappings']:
        profile, plugin = entry['profile'], entry['plugin']
        profile_path, plugin_path = safe_parts(profile), safe_parts(plugin)
        if len(profile_path.parts) != 1 or plugin_path.name in MANUAL:
            raise ValueError('Excluded profile or plugin')
        if (profile, plugin) in seen:
            raise ValueError('Duplicate mapping')
        seen.add((profile, plugin))
        if entry['source'] != plugin:
            raise ValueError('Each plugin must use its canonical repository source')
        source = repo / safe_parts(entry['source'])
        if not source.is_dir() or source.is_symlink() or not source.resolve().is_relative_to(repo):
            raise ValueError('Missing or redirected source')
        if manifest_version(source) != entry['version']:
            raise ValueError('Version mismatch')
        base = home if profile == 'default' else home / 'profiles' / profile
        target = base / 'plugins' / plugin_path
        # Explicit entries only. Never enumerate profile/backup/tombstone trees.
        if not target.parent.is_dir() or target.parent.resolve() != target.parent.absolute():
            raise ValueError('Missing or redirected install parent')
        if target.is_symlink():
            if target.resolve() != source:
                raise ValueError('Divergent symlink')
        elif target.exists():
            raise ValueError('Real directory requires verified migration')
        else:
            plans.append((target, source))
    created = []
    if apply:
        try:
            for target, source in plans:
                target.symlink_to(os.path.relpath(source, target.parent))
                created.append(target)
                if not target.is_symlink() or target.resolve() != source:
                    raise RuntimeError('Readback failed')
        except Exception:
            for target in reversed(created):
                target.unlink()
            raise
    return {'mappings': len(seen), 'unchanged': len(seen) - len(plans),
            'created' if apply else 'would_create': len(plans)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--dry-run', action='store_true')
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('HERMES_HOME', Path.home() / '.hermes')))
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--mapping', type=Path, default=Path(__file__).with_name('profile-plugins.json'))
    args = parser.parse_args()
    try:
        mapping = json.loads(args.mapping.read_text(encoding='utf-8'))
        print(json.dumps(reconcile(args.repo, args.home, mapping, args.apply), sort_keys=True))
    except (ValueError, OSError, KeyError, RuntimeError):
        parser.exit(1, 'Validation/deployment failed; inspect manifest locally.\n')


if __name__ == '__main__':
    main()
