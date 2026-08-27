"""Sound-clip discovery and behavior-aware selection.

Clips live in ``sounds/`` as ``.mp3`` / ``.wav`` files. Tagging is *gradual*:
``sounds/tags.yaml`` maps a filename to the behavior tags it fits. A clip that is
not listed there is "untagged" and serves as a general-purpose fallback.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

logger = logging.getLogger("barkbox.clips")

_AUDIO_SUFFIXES = {".mp3", ".wav"}


def load_clips(sounds_dir: str | Path) -> list[Path]:
    """Return every audio file directly under ``sounds_dir``, sorted by name."""
    d = Path(sounds_dir)
    if not d.is_dir():
        logger.warning("sounds dir %s does not exist", d)
        return []
    return sorted(
        (p for p in d.iterdir() if p.is_file() and p.suffix.lower() in _AUDIO_SUFFIXES),
        key=lambda p: p.name.lower(),
    )


def load_tags(tags_path: str | Path) -> dict[str, list[str]]:
    """Read ``tags.yaml`` into ``{filename: [tag, ...]}``.

    A missing, empty or malformed file yields ``{}`` (tagging is optional).
    """
    p = Path(tags_path)
    if not p.is_file():
        return {}
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.error("could not parse %s: %s", p, exc)
        return {}
    if not isinstance(raw, dict):
        logger.error("%s: expected a mapping of filename -> tags", p)
        return {}
    out: dict[str, list[str]] = {}
    for name, tags in raw.items():
        if isinstance(tags, str):
            tags = [tags]
        if isinstance(tags, list):
            out[str(name)] = [str(t) for t in tags]
    return out


def resolve_clips(
    all_clips: list[Path],
    tags: dict[str, list[str]],
    behavior_tags: list[str],
) -> list[Path]:
    """Clips usable for a behavior, with a fallback chain.

    1. Clips whose tags intersect ``behavior_tags``.
    2. Otherwise the *general pool* — every clip not listed in ``tags`` at all.
    3. Otherwise every clip (better a repeat than silence).
    """
    wanted = set(behavior_tags)
    matched = [c for c in all_clips if wanted & set(tags.get(c.name, ()))]
    if matched:
        return matched
    untagged = [c for c in all_clips if c.name not in tags]
    if untagged:
        return untagged
    return list(all_clips)


def count_tagged(all_clips: list[Path], tags: dict[str, list[str]]) -> int:
    """How many present clips carry at least one tag (for the status endpoint)."""
    return sum(1 for c in all_clips if tags.get(c.name))
