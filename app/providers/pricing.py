"""What paid calls cost, for reserving budget before a call and settling it after.

Claude prices are US dollars per million tokens (Anthropic's list prices, October 2026). Thinking tokens are billed
as output. Update this table when prices change; unknown models are charged at the most expensive rate we use, so a
typo can't make a run look cheaper than it is.
"""

from dataclasses import dataclass
from decimal import Decimal

MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class TokenPrice:
    input: Decimal
    output: Decimal
    cache_read: Decimal

    @property
    def cache_write(self) -> Decimal:
        return self.input * Decimal("1.25")  # 5-minute cache writes cost a quarter more than plain input


CLAUDE = {
    "claude-opus-5-5": TokenPrice(Decimal("4.00"), Decimal("20.00"), Decimal("0.20")),
    "claude-sonnet-5-5": TokenPrice(Decimal("2.00"), Decimal("10.00"), Decimal("0.20")),
    "claude-haiku-5-5": TokenPrice(Decimal("0.10"), Decimal("0.50"), Decimal("0.01")),
}
MOST_EXPENSIVE = CLAUDE["claude-opus-5-5"]
WEB_SEARCH_USD = Decimal("0.01")  # $10 per 1,000 searches

APIFY_POST_USD = Decimal("0.002")
APIFY_EMPTY_QUERY_USD = Decimal("0.001")  # a search that finds nothing is still charged


def claude_price(model: str) -> TokenPrice:
    return CLAUDE.get(model, MOST_EXPENSIVE)


def claude_cost(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    web_searches: int = 0,
) -> Decimal:
    price = claude_price(model)
    tokens = (
        price.input * input_tokens
        + price.output * output_tokens
        + price.cache_read * cache_read_tokens
        + price.cache_write * cache_write_tokens
    )
    return (tokens / MILLION + WEB_SEARCH_USD * web_searches).quantize(Decimal("0.0001"))


LABEL_PROMPT_TOKENS = 1_200  # the labeling instructions, sent with every batch
LABEL_TOKENS_PER_POST = 600  # up to 1,500 characters of post text, the headline and the tags around them
LABEL_THINKING_TOKENS = 2_000  # what a call at low effort may spend thinking
LABEL_OUTPUT_PER_POST = 150  # a label, a confidence and a short reason


def claude_label_estimate(model: str, posts: int) -> Decimal:
    """A generous estimate for labeling one batch of posts: reserved before the call, settled at the real usage."""
    return claude_cost(
        model,
        input_tokens=LABEL_PROMPT_TOKENS + LABEL_TOKENS_PER_POST * posts,
        output_tokens=LABEL_THINKING_TOKENS + LABEL_OUTPUT_PER_POST * posts,
    )


def apify_search_estimate(queries: int, max_posts_per_query: int) -> Decimal:
    """The most a post search can charge: every query returns its maximum (or nothing, at the empty-query price)."""
    per_query = max(APIFY_POST_USD * max_posts_per_query, APIFY_EMPTY_QUERY_USD)
    return per_query * queries
