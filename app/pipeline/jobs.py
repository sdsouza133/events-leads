"""Every kind of job the worker runs, by name."""

from app.pipeline import analyze, collect, qualify
from app.pipeline.base import JobKind

JOBS: dict[str, JobKind] = {
    analyze.ANALYZE_SITE: JobKind(analyze.analyze_site, on_give_up=analyze.give_up),
    analyze.FIND_EVENTS: JobKind(analyze.find_events, on_give_up=analyze.give_up),
    collect.COLLECT_EVENT: JobKind(collect.collect_event, on_give_up=collect.give_up),
    qualify.QUALIFY: JobKind(qualify.qualify, on_give_up=qualify.give_up),
}
