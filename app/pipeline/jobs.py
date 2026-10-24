"""Every kind of job the worker runs, by name."""

from app.pipeline import analyze
from app.pipeline.base import JobKind

JOBS: dict[str, JobKind] = {
    analyze.ANALYZE_SITE: JobKind(analyze.analyze_site, on_give_up=analyze.give_up),
    analyze.FIND_EVENTS: JobKind(analyze.find_events, on_give_up=analyze.give_up),
}
