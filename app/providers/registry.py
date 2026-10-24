"""Pick real services or fakes from the settings (ADR 0003)."""

from dataclasses import dataclass

import anthropic

from app.config import Settings
from app.providers import fakes
from app.providers.anthropic_llm import AnthropicLlm
from app.providers.apify import ApifyPostSearch
from app.providers.apollo import ApolloEnrichment
from app.providers.outbox import OutboxMailer
from app.providers.protocols import EmailVerifier, Llm, Mailer, PeopleEnrichment, PostSearch, WebFetcher
from app.providers.reoon import ReoonVerifier
from app.providers.resend import ResendMailer
from app.providers.webfetch import SafeFetcher


class ProviderConfigError(ValueError):
    """Real providers were asked for, but a key they need isn't set."""


@dataclass(frozen=True)
class Providers:
    fetcher: WebFetcher
    llm: Llm
    search: PostSearch
    enrichment: PeopleEnrichment
    verifier: EmailVerifier | None  # None: the optional second email check is off
    mailer: Mailer


def build_providers(settings: Settings) -> Providers:
    if settings.providers == "fake":
        return Providers(
            fetcher=fakes.FakeFetcher(),
            llm=fakes.FakeLlm(),
            search=fakes.FakePostSearch(),
            enrichment=fakes.FakeEnrichment(),
            verifier=fakes.FakeEmailVerifier() if settings.reoon_enabled else None,
            mailer=OutboxMailer(),  # kept for the outbox page, where you sign in when working locally
        )

    required = {
        "ANTHROPIC_API_KEY": settings.anthropic_api_key,
        "APIFY_TOKEN": settings.apify_token,
        "APOLLO_API_KEY": settings.apollo_api_key,
    }
    if settings.reoon_enabled:
        required["REOON_API_KEY"] = settings.reoon_api_key
    if settings.env == "production":
        required["RESEND_API_KEY"] = settings.resend_api_key
    missing = sorted(name for name, value in required.items() if value is None)
    if missing:
        raise ProviderConfigError("PROVIDERS=real needs " + ", ".join(missing))

    def secret(value: object) -> str:
        return value.get_secret_value()  # type: ignore[attr-defined, no-any-return]

    llm = AnthropicLlm(
        # No retries inside the SDK: an attempt that timed out may have been billed, and its cost has to reach the
        # budget (ADR 0002). The job queue retries instead, accounting for each attempt.
        client=anthropic.Anthropic(api_key=secret(settings.anthropic_api_key), max_retries=0),
        analyze_model=settings.analyze_model,
        classify_model=settings.classify_model,
        analyze_effort=settings.analyze_effort,
        classify_effort=settings.classify_effort,
        max_web_searches=settings.max_web_searches,
        fallbacks=settings.llm_fallbacks,
    )
    mailer: Mailer = (
        ResendMailer(secret(settings.resend_api_key), sender=settings.mail_from)
        if settings.resend_api_key is not None
        else OutboxMailer()  # only outside production (checked above): previews keep emails in the outbox
    )
    return Providers(
        fetcher=SafeFetcher(),
        llm=llm,
        search=ApifyPostSearch(
            secret(settings.apify_token), actor=settings.apify_actor, posted_limit=settings.apify_posted_limit
        ),
        enrichment=ApolloEnrichment(secret(settings.apollo_api_key)),
        verifier=ReoonVerifier(secret(settings.reoon_api_key)) if settings.reoon_enabled else None,
        mailer=mailer,
    )
