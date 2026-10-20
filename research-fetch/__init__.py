"""Research CLI plugin registration."""

from hermes_constants import get_hermes_home

from . import arxiv_search, youtube_transcript


def register(ctx):
    youtube_transcript.register(ctx)
    arxiv_search.register(ctx)
    skills_dir = get_hermes_home() / 'skills'
    for name, relative_path in (
        ('youtube-transcript', 'media/youtube-transcript/SKILL.md'),
        ('arxiv-research', 'research/arxiv-research/SKILL.md'),
    ):
        skill_md = skills_dir / relative_path
        if skill_md.is_file():
            ctx.register_skill(name, skill_md)
