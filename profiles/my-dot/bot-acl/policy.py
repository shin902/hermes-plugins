"""Canonical-ID ACL using public home resolution and on-disk configuration."""

import re
from pathlib import Path


def _read_mapping(path):
    import hermes_yaml as yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("configuration must be a mapping")
    return data


def _local_profile_exists(root, name):
    # Both profile.yaml and config.yaml identify real profiles. Older profiles
    # may have configuration but no metadata file.
    home = root if name == "default" else root / "profiles" / name
    if name != "default" and (root / "profiles" / ".deleted" / name).exists():
        return False
    return home.is_dir() and any((home / marker).is_file() for marker in ("profile.yaml", "config.yaml"))


def check(domains, tool_name, args, **kwargs):
    if tool_name != "message_agent":
        return None

    source = target = None
    reason = "could not verify local profile domain membership"
    try:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
        if home.parent.name == "profiles":
            source, root = home.name, home.parent.parent
        else:
            source, root = "default", home
        raw = (args or {}).get("target")
        if not isinstance(raw, str):
            raise ValueError("target must be a string")
        raw = raw.strip()
        raw = raw[1:] if raw.startswith("@") else raw
        # Only canonical lowercase IDs and the reserved default handle.
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", raw):
            reason = "use a canonical local profile ID; aliases and qualified targets are forbidden"
        else:
            target = "default" if raw == "hermes" else raw
            source_domains = {name for name, members in domains.items() if source in members}
            target_domains = {name for name, members in domains.items() if target in members}
            if source == target:
                reason = "self-targets may route to another connection"
            elif len(source_domains) != 1 or source_domains != target_domains:
                reason = "source and target must belong to the same single group"
            elif not (_local_profile_exists(root, source) and _local_profile_exists(root, target)):
                reason = "local source or target profile is absent or deleted"
            else:
                # The built-in router reads root bot_peers before local IDs.
                peers = _read_mapping(root / "config.yaml").get("bot_peers")
                if peers is None:
                    peers = {}
                if not isinstance(peers, dict) or not all(isinstance(key, str) for key in peers):
                    raise ValueError("bot_peers must be a string-keyed mapping")
                if raw in {key.lower() for key in peers}:
                    reason = "target collides with an external peer name"
                else:
                    return None
    except Exception:
        reason = "profile/config verification failed"

    return {
        "action": "block",
        "message": (
            f"Bot-to-bot message blocked by bot-acl security policy "
            f"(source={source!r}, target={target!r}): {reason}."
        ),
    }
