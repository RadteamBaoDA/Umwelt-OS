"""Free-provider catalog facts, eligibility rules and durable quota policy.

Pure data and decisions only: no I/O, no secrets. Availability of an endpoint is not a
license grant, and nothing here asserts runtime acceptance. Facts were checked on
``CHECKED_ON`` against the policy in ``docs/free-data-provider-policy.md``; unknown official
caps are recorded as ``limit_units=None`` (never unlimited).
"""

from dataclasses import dataclass
from typing import Literal

CHECKED_ON = "2026-10-07"
QUOTA_POLICY_REVISION = 1

DeploymentUse = Literal["personal", "noncommercial", "commercial", "unknown"]
DEPLOYMENT_USES: tuple[str, ...] = ("personal", "noncommercial", "commercial", "unknown")
Eligibility = Literal["open", "personal", "noncommercial", "review"]
_USE_RANK = {"personal": 0, "noncommercial": 1, "commercial": 2}

TERMS_INELIGIBLE = "provider_terms_ineligible"


@dataclass(frozen=True)
class QuotaWindowPolicy:
    """One durable budget window; ``limit_units=None`` means counted with no enforced cap."""
    policy_key: str
    budget_kind: Literal["provider", "credential", "ip"]
    window: Literal["second", "minute", "day", "month"]
    unit: Literal["http_calls", "request_weight", "call_credits"]
    limit_units: int | None
    cost_per_send: int = 1
    basis: Literal["pilot_local", "official_documented", "official_unknown"] = "pilot_local"


@dataclass(frozen=True)
class FreeProviderSpec:
    """Catalog facts and execution bounds; availability is not a license grant."""
    id: str
    hosts: tuple[str, ...]
    data_kind: Literal["news", "measurement", "event"]
    key_required: bool
    default_interval_minutes: int
    terms_url: str
    eligibility: Literal["open", "personal", "noncommercial", "review"]
    attribution: str
    execution: Literal["supported", "experimental", "research_only"]
    label: str = ""
    endpoints: tuple[str, ...] = ()
    quota: tuple[QuotaWindowPolicy, ...] = ()
    # Evidence kinds stay separate; no field implies another. Adapters ship in P2/P3/C3.
    endpoint_probe: Literal["plan_documented", "unverified"] = "plan_documented"
    code_implemented: bool = False
    runtime_verified: bool = False
    checked_on: str = CHECKED_ON
    secret_location: Literal["header", "query"] | None = None


# Providers the shared executor can actually dispatch (collection.ADAPTERS). CoinGecko is absent: its
# demo key has no credential slot yet. This is code presence only; runtime_verified stays False.
DISPATCHABLE = frozenset({
    "bbc_world", "vnexpress_business", "hn_top", "gdelt_economy",
    "world_bank", "frankfurter", "ecb", "binance", "alternative_me", "usgs", "coinpaprika",
})


def _unknown(kind: str = "provider", unit: str = "http_calls", cost: int = 1) -> QuotaWindowPolicy:
    return QuotaWindowPolicy(
        "official_unknown_calls_utc_day", kind, "day", unit, None, cost, "official_unknown",  # type: ignore[arg-type]
    )


def _day(limit: int, kind: str = "provider", unit: str = "http_calls", cost: int = 1,
         key: str = "pilot_calls_utc_day") -> QuotaWindowPolicy:
    return QuotaWindowPolicy(key, kind, "day", unit, limit, cost)  # type: ignore[arg-type]


def _spec(id_: str, label: str, hosts: tuple[str, ...], kind: str, interval: int, terms_url: str,
          eligibility: str, attribution: str, endpoints: tuple[str, ...], quota: tuple[QuotaWindowPolicy, ...],
          execution: str = "supported", key: bool = False, secret: str | None = None,
          implemented: bool = False) -> FreeProviderSpec:
    return FreeProviderSpec(
        id=id_, hosts=hosts, data_kind=kind, key_required=key, default_interval_minutes=interval,  # type: ignore[arg-type]
        terms_url=terms_url, eligibility=eligibility, attribution=attribution, execution=execution,  # type: ignore[arg-type]
        label=label, endpoints=endpoints, quota=quota, secret_location=secret,  # type: ignore[arg-type]
        code_implemented=implemented or id_ in DISPATCHABLE,
    )


FREE_PROVIDER_SPECS: tuple[FreeProviderSpec, ...] = (
    _spec("bbc_world", "BBC World News", ("feeds.bbci.co.uk",), "news", 30,
          "https://www.bbc.co.uk/usingthebbc/terms/", "review",
          "Display BBC as publisher with a link to the original article; feed-only title, summary and link.",
          ("/news/world/rss.xml",), (_day(96), _unknown())),
    _spec("vnexpress_business", "VnExpress Business", ("vnexpress.net",), "news", 30,
          "https://vnexpress.net/rss", "noncommercial",
          "Identify VnExpress clearly and link the original article; feed-only scope.",
          ("/rss/kinh-doanh.rss",), (_day(96), _unknown())),
    _spec("hn_top", "Hacker News top stories", ("hacker-news.firebaseio.com",), "news", 60,
          "https://github.com/HackerNews/API", "review",
          "Attribute Hacker News plus the item and publisher URL; the API license does not license linked articles.",
          ("/v0/topstories.json", "/v0/item/{id}.json"),
          (_day(528), QuotaWindowPolicy("pilot_calls_utc_minute", "provider", "minute", "http_calls", 12))),
    _spec("gdelt_economy", "GDELT economy articles", ("api.gdeltproject.org",), "news", 60,
          "https://gdeltproject.org/about.html", "review",
          "Cite the GDELT Project and the publisher URL; article metadata is not republication permission.",
          ("/api/v2/doc/doc?query=economy&mode=artlist&format=json&maxrecords=5&timespan=24h",),
          (_day(48), _unknown()), execution="experimental"),
    _spec("world_bank", "World Bank annual GDP (Vietnam)", ("api.worldbank.org",), "measurement", 1440,
          "https://datacatalog.worldbank.org/public-licenses", "open",
          "Credit the World Bank and the original indicator; retain the dataset license and changed notice.",
          ("/v2/country/VN/indicator/NY.GDP.MKTP.CD?format=json&per_page=3",), (_day(32), _unknown())),
    _spec("frankfurter", "Frankfurter USD/VND", ("api.frankfurter.dev",), "measurement", 1440,
          "https://frankfurter.dev/license/", "open",
          "Credit Frankfurter and contributing providers; the default rate is a blended rate, not a single-bank rate.",
          ("/v2/rate/usd/vnd",), (_day(8), _unknown())),
    _spec("ecb", "ECB euro reference rates", ("www.ecb.europa.eu",), "measurement", 1440,
          "https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html", "open",
          "Credit the ECB; preserve the rate date and identify transformations.",
          ("/stats/eurofxref/eurofxref-daily.xml",), (_day(8), _unknown())),
    _spec("binance", "Binance BTCUSDT price", ("data-api.binance.vision",), "measurement", 15,
          "https://developers.binance.com/en/docs/products/spot/rest-api", "review",
          "Credit Binance; BTCUSDT market, USDT quote. No trading endpoints or keys.",
          ("/api/v3/ticker/price?symbol=BTCUSDT",),
          (_day(384, unit="request_weight", cost=2, key="pilot_request_weight_utc_day"),
           QuotaWindowPolicy("pilot_request_weight_utc_minute", "provider", "minute", "request_weight", 4, 2),
           QuotaWindowPolicy("official_request_weight_unknown", "ip", "minute", "request_weight", None, 2,
                             "official_unknown"))),
    _spec("alternative_me", "Alternative.me Fear and Greed", ("api.alternative.me",), "measurement", 1440,
          "https://alternative.me/crypto/fear-and-greed-index/", "open",
          "Display Alternative.me with a link adjacent to the index, including translated output.",
          ("/fng/?limit=2",), (_day(8), _unknown())),
    _spec("usgs", "USGS M4.5+ earthquakes (day)", ("earthquake.usgs.gov",), "event", 60,
          "https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits", "open",
          "Credit the U.S. Geological Survey with the event citation; do not fetch detail URLs automatically.",
          ("/earthquakes/feed/v1.0/summary/4.5_day.geojson",), (_day(48), _unknown())),
    _spec("coinpaprika", "CoinPaprika BTC ticker", ("api.coinpaprika.com",), "measurement", 15,
          "https://docs.coinpaprika.com/api-plans", "personal",
          "Credit CoinPaprika; free use is personal with no redistribution.",
          ("/v1/tickers/btc-bitcoin",),
          (_day(192), QuotaWindowPolicy("pilot_calls_utc_second", "provider", "second", "http_calls", 1),
           QuotaWindowPolicy("official_calls_per_second", "ip", "second", "http_calls", 10, 1, "official_documented"),
           QuotaWindowPolicy("official_calls_per_month", "ip", "month", "http_calls", 20_000, 1, "official_documented"))),
    _spec("coingecko", "CoinGecko BTC price (Demo)", ("api.coingecko.com",), "measurement", 15,
          "https://www.coingecko.com/en/api_terms", "review",
          "Show Powered by CoinGecko with a link per the API terms and brand guide.",
          ("/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",),
          (_day(192, kind="credential"),
           QuotaWindowPolicy("pilot_calls_utc_second", "credential", "second", "http_calls", 1),
           QuotaWindowPolicy("official_calls_per_minute", "credential", "minute", "http_calls", 100, 1,
                             "official_documented"),
           QuotaWindowPolicy("official_call_credits_per_month", "credential", "month", "call_credits", 10_000, 1,
                             "official_documented")),
          key=True, secret="header"),
    # Existing C1 adapter. 12/UTC-day is the conservative pilot cap until the provider reset
    # anchor is evidenced; the official 25/day fact is recorded separately.
    _spec("alpha_vantage", "Alpha Vantage daily equities", ("www.alphavantage.co",), "measurement", 1440,
          "https://www.alphavantage.co/terms_of_service/", "personal",
          "Credit Alpha Vantage with symbol and day; no free realtime entitlement.",
          ("/query?function=TIME_SERIES_DAILY&symbol={symbol}&outputsize=compact",),
          (_day(12, kind="credential"),
           QuotaWindowPolicy("pilot_calls_utc_minute", "credential", "minute", "http_calls", 1),
           QuotaWindowPolicy("official_calls_per_day", "credential", "day", "http_calls", 25, 1,
                             "official_documented")),
          key=True, secret="query", implemented=True),
)

_BY_ID = {spec.id: spec for spec in FREE_PROVIDER_SPECS}
assert len(_BY_ID) == len(FREE_PROVIDER_SPECS)  # import-time catalog invariant


def get_provider_spec(provider_id: str | None) -> FreeProviderSpec | None:
    """Return the exact free-provider spec, or None for any other provider."""
    return _BY_ID.get(provider_id or "")


def deployment_use_matches(eligibility: str, deployment_use: str) -> bool:
    """Whether a catalog eligibility class admits the declared deployment use.

    open admits any declared use (attribution duties remain); personal admits only personal;
    noncommercial admits personal or noncommercial; review (and anything unrecognised) is always
    False here because it needs a separate persisted operator review.
    """
    if eligibility == "open":
        return True
    if eligibility == "personal":
        return deployment_use == "personal"
    if eligibility == "noncommercial":
        return deployment_use in {"personal", "noncommercial"}
    return False


@dataclass(frozen=True)
class OperatorReview:
    """Operator-recorded review evidence; never derived from an owner tick."""
    state: Literal["pending", "approved", "rejected"]
    allowed_use: str | None = None
    evidence_ref: str | None = None
    reviewed_terms_version: str | None = None


@dataclass(frozen=True)
class TermsDecision:
    """Eligibility outcome; ``code`` is a stable machine reason."""
    allowed: bool
    code: Literal["ok", "terms_not_acknowledged", "use_incompatible", "operator_review_required"]


def evaluate_terms(
    spec: FreeProviderSpec, *, declared_use: str | None, acknowledged_terms_version: str | None,
    review: OperatorReview | None = None,
) -> TermsDecision:
    """Deny by default. The single check shared by catalog readiness and source activation."""
    if not acknowledged_terms_version or declared_use not in DEPLOYMENT_USES:
        return TermsDecision(False, "terms_not_acknowledged")
    if spec.eligibility != "review":
        ok = deployment_use_matches(spec.eligibility, declared_use)
        return TermsDecision(ok, "ok" if ok else "use_incompatible")
    if (
        review is None or review.state != "approved" or not review.evidence_ref
        or review.reviewed_terms_version != acknowledged_terms_version
        or review.allowed_use not in _USE_RANK
    ):
        return TermsDecision(False, "operator_review_required")
    ok = declared_use in _USE_RANK and _USE_RANK[declared_use] <= _USE_RANK[review.allowed_use]
    return TermsDecision(ok, "ok" if ok else "use_incompatible")
