from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.linkedin import normalize_profile_url
from app.providers import fakes, pricing
from app.providers.anthropic_llm import AnthropicLlm
from app.providers.apify import ApifyPostSearch
from app.providers.outbox import OutboxMailer
from app.providers.registry import ProviderConfigError, build_providers
from app.providers.resend import ResendMailer


def test_fake_providers_by_default() -> None:
    providers = build_providers(Settings())
    assert isinstance(providers.llm, fakes.FakeLlm)
    assert providers.verifier is None
    assert isinstance(providers.mailer, OutboxMailer)  # emails go to the outbox page, where you sign in locally


def test_real_providers_need_their_keys() -> None:
    with pytest.raises(ProviderConfigError, match="ANTHROPIC_API_KEY, APIFY_TOKEN, APOLLO_API_KEY"):
        build_providers(Settings(providers="real"))


def test_real_providers_with_keys() -> None:
    settings = Settings(
        providers="real",
        anthropic_api_key="a",
        apify_token="b",
        apollo_api_key="c",
    )
    providers = build_providers(settings)
    assert isinstance(providers.llm, AnthropicLlm)
    assert isinstance(providers.search, ApifyPostSearch)
    assert isinstance(providers.mailer, OutboxMailer)  # no Resend key outside production: kept, not sent
    with_mail = build_providers(settings.model_copy(update={"resend_api_key": settings.apollo_api_key}))
    assert isinstance(with_mail.mailer, ResendMailer)


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_key_counts_as_missing(monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", blank)  # e.g. a CI secret or Railway variable that was never filled in
    with pytest.raises(ProviderConfigError, match=r"needs ANTHROPIC_API_KEY$"):
        build_providers(Settings(providers="real", apify_token="b", apollo_api_key="c"))


def test_reoon_key_is_required_only_when_enabled() -> None:
    settings = Settings(
        providers="real", anthropic_api_key="a", apify_token="b", apollo_api_key="c", reoon_enabled=True
    )
    with pytest.raises(ProviderConfigError, match="REOON_API_KEY"):
        build_providers(settings)


def test_production_refuses_fake_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="PROVIDERS=real"):
        Settings(env="production", secret_key="k" * 48, database_url="postgresql://u:p@db.example/x")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.linkedin.com/in/Jane-Doe-123/", "https://www.linkedin.com/in/jane-doe-123"),
        ("http://linkedin.com/in/jane-doe-123?trk=x", "https://www.linkedin.com/in/jane-doe-123"),
        ("https://ca.linkedin.com/in/jane-doe-123", "https://www.linkedin.com/in/jane-doe-123"),
        ("https://www.linkedin.com/pub/jane-doe/1/2/3", "https://www.linkedin.com/in/jane-doe"),
        ("https://www.linkedin.com/company/acme", None),
        ("https://evil.example/in/jane", None),
        ("https://linkedin.com.evil.example/in/jane", None),
        ("", None),
        (None, None),
    ],
)
def test_profile_urls_are_normalized(url: str | None, expected: str | None) -> None:
    assert normalize_profile_url(url) == expected


def test_claude_cost_counts_every_kind_of_token_and_search() -> None:
    cost = pricing.claude_cost(
        "claude-opus-5-5",
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=100_000,
        web_searches=3,
    )
    assert cost == Decimal("4.00") + Decimal("2.00") + Decimal("0.20") + Decimal("0.50") + Decimal("0.03")


def test_unknown_models_are_priced_at_the_top_rate() -> None:
    assert pricing.claude_price("claude-typo") == pricing.CLAUDE["claude-opus-5-5"]


def test_labeling_a_batch_is_estimated_from_its_size_and_model() -> None:
    # 25 posts on Opus: 16,200 tokens in at $4/M and 5,750 out at $20/M.
    assert pricing.claude_label_estimate("claude-opus-5-5", 25) == Decimal("0.1798")
    assert pricing.claude_label_estimate("claude-haiku-5-5", 25) < pricing.claude_label_estimate("claude-opus-5-5", 25)


def test_apify_estimate_is_the_worst_case() -> None:
    assert pricing.apify_search_estimate(queries=4, max_posts_per_query=50) == Decimal("0.400")
    assert pricing.apify_search_estimate(queries=2, max_posts_per_query=0) == Decimal("0.002")
