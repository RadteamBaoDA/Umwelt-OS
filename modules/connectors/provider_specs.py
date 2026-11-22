"""Free-provider catalog facts, eligibility rules and durable quota policy.

Pure data and decisions only: no I/O, no secrets. Availability of an endpoint is not a
license grant, and nothing here asserts runtime acceptance. Facts were checked on
``CHECKED_ON`` against the policy in ``docs/free-data-provider-policy.md``; unknown official
caps are recorded as ``limit_units=None`` (never unlimited).
"""

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Literal

from modules.connectors.provider_eligibility import DEPLOYMENT_USES as DEPLOYMENT_USES
from modules.connectors.provider_eligibility import OperatorReview as OperatorReview
from modules.connectors.provider_eligibility import TermsDecision as TermsDecision
from modules.connectors.provider_eligibility import (
    deployment_use_matches as deployment_use_matches,
)
from modules.connectors.provider_eligibility import evaluate_terms as evaluate_terms
from modules.connectors.provider_options import option_fields
from modules.connectors.provider_specs_media import MEDIA_KEYED_SPECS
from modules.connectors.provider_specs_tables import (
    _EXPANSION_IDS,
    _LONG_RETENTION_IDS,
    _NON_FREE_QUOTA_POLICIES,
    _REALTIME_TIER_IDS,
    PROVIDER_MIN_INTERVAL_MINUTES,
    _day,
    _spec,
    _unknown,
)
from modules.connectors.provider_specs_tables import (
    CHECKED_ON as CHECKED_ON,
)
from modules.connectors.provider_specs_tables import (
    DISPATCHABLE as DISPATCHABLE,
)
from modules.connectors.provider_specs_tables import (
    FreeProviderSpec as FreeProviderSpec,
)
from modules.connectors.provider_specs_tables import (
    QuotaWindowPolicy as QuotaWindowPolicy,
)
from modules.connectors.sections import MAX_SECTIONS, SECTION_CATALOG

QUOTA_POLICY_REVISION = 1

DeploymentUse = Literal["personal", "noncommercial", "commercial", "unknown"]
Eligibility = Literal["open", "personal", "noncommercial", "review"]

TERMS_INELIGIBLE = "provider_terms_ineligible"

# Self-imposed pilot cap for section-catalog RSS presets: one GET per selected section per sync, so six sections
# at the 15-minute floor would still fit; every send is debited against it. Not an upstream limit.
SECTION_DAILY_CAP = 96 * MAX_SECTIONS


DEFAULT_MIN_INTERVAL_MINUTES = 15
# Floors at or above this are slow/unofficial sources: manual "Collect now" is rate-limited to the floor too.
COLLECT_COOLDOWN_FLOOR_MINUTES = 360


def min_interval_minutes(provider: str | None) -> int:
    """Return the hard minimum schedule interval in minutes for a provider.

    Providers with no entry (generic rss/web/rest/mcp sources, owner-credential and real-time providers) get the
    15-minute default, so the clamp is a no-op for them.
    """
    return PROVIDER_MIN_INTERVAL_MINUTES.get(provider or "", DEFAULT_MIN_INTERVAL_MINUTES)


def collection_cooldown_seconds(
    provider: str | None, last_started: datetime | None, now: datetime
) -> int:
    """Return how many seconds a manual "Collect now" must still wait, or 0 when it may proceed.

    Only slow/unofficial providers (floor at least ``COLLECT_COOLDOWN_FLOOR_MINUTES``) are throttled, to the
    same floor as their schedule; every other provider (and a source never collected) always returns 0.
    """
    floor = min_interval_minutes(provider)
    if last_started is None or floor < COLLECT_COOLDOWN_FLOOR_MINUTES:
        return 0
    return max(0, int(floor * 60 - (now - last_started).total_seconds()))


_SPECS_RAW: tuple[FreeProviderSpec, ...] = (
    _spec(
        "anilist_anime",
        "AniList anime",
        "media",
        ("graphql.anilist.co",),
        "event",
        30,
        "https://anilist.co/terms",
        "noncommercial",
        "Data via AniList; link the original AniList entry.",
        ("/",),
        (
            QuotaWindowPolicy("pilot_calls_utc_minute", "provider", "minute", "http_calls", 60),
            _unknown(),
        ),
    ),
    _spec(
        "anilist_manga",
        "AniList manga",
        "media",
        ("graphql.anilist.co",),
        "event",
        60,
        "https://anilist.co/terms",
        "noncommercial",
        "Data via AniList; link the original AniList entry.",
        ("/",),
        (
            QuotaWindowPolicy("pilot_calls_utc_minute", "provider", "minute", "http_calls", 60),
            _unknown(),
        ),
    ),
    _spec(
        "livechart_episodes",
        "LiveChart anime episodes",
        "media",
        ("www.livechart.me",),
        "event",
        30,
        "https://www.livechart.me/terms",
        "review",
        "Data via LiveChart.me; link the original episode.",
        ("/feeds/episodes",),
        (_day(48), _unknown()),
    ),
    replace(
        _spec(
            "tvmaze_schedule",
            "TVMaze schedule",
            "media",
            ("api.tvmaze.com",),
            "event",
            60,
            "https://www.tvmaze.com/api#licensing",
            "review",
            "TVMaze (CC BY-SA 4.0); link the original TVMaze episode and disclose processing.",
            ("/schedule", "/schedule/web", "/shows"),
            (
                # Followed shows cost one call per show id (max 20) per sync; TVMaze allows 20 calls/10 s.
                _day(600),
                QuotaWindowPolicy("pilot_calls_utc_minute", "provider", "minute", "http_calls", 20),
                _unknown(),
            ),
        ),
        checked_on="2026-11-17",
    ),
    _spec(
        "openlibrary_books",
        "Open Library books",
        "media",
        ("openlibrary.org", "covers.openlibrary.org"),
        "event",
        360,
        "https://openlibrary.org/developers/api",
        "open",
        "Data via Open Library (CC0 1.0); link the work record.",
        ("/search.json", "/subjects", "/recentchanges.json", "/works"),
        (
            _day(48),
            QuotaWindowPolicy(
                "openlibrary_calls_utc_second", "provider", "second", "http_calls", 2
            ),
            QuotaWindowPolicy(
                "openlibrary_calls_utc_minute", "provider", "minute", "http_calls", 150
            ),
            _unknown(),
        ),
    ),
    _spec(
        "gutendex_books",
        "Gutendex public domain books",
        "media",
        ("gutendex.com",),
        "event",
        1440,
        "https://gutendex.com/",
        "review",
        "Project Gutenberg public domain ebook metadata; link the original Gutenberg record.",
        ("/books/",),
        (
            _day(48),
            QuotaWindowPolicy("gutendex_calls_utc_second", "provider", "second", "http_calls", 1),
            QuotaWindowPolicy("gutendex_calls_utc_minute", "provider", "minute", "http_calls", 60),
            _unknown(),
        ),
    ),
    replace(
        _spec(
            "ann_news",
            "Anime News Network",
            "media",
            ("www.animenewsnetwork.com", "cdn.animenewsnetwork.com"),
            "event",
            60,
            "https://www.animenewsnetwork.com/encyclopedia/api",
            "review",
            "Data via Anime News Network; link the original entry.",
            ("/news/atom.xml", "/encyclopedia/reports.xml"),
            (
                QuotaWindowPolicy("ann_calls_utc_second", "provider", "second", "http_calls", 1),
                QuotaWindowPolicy("ann_calls_utc_minute", "provider", "minute", "http_calls", 60),
                _unknown(),
            ),
        ),
        checked_on="2026-11-17",
    ),
    replace(
        _spec(
            "mangaupdates_releases",
            "MangaUpdates releases",
            "media",
            ("api.mangaupdates.com",),
            "event",
            60,
            "https://api.mangaupdates.com/",
            "review",
            "Data via MangaUpdates; attribution required and link the original series.",
            ("/v1/releases/search",),
            (
                QuotaWindowPolicy(
                    "mangaupdates_calls_utc_second", "provider", "second", "http_calls", 1
                ),
                QuotaWindowPolicy(
                    "mangaupdates_calls_utc_minute", "provider", "minute", "http_calls", 60
                ),
                _unknown(),
            ),
        ),
        checked_on="2026-11-17",
    ),
    replace(
        _spec(
            "mangadex_releases",
            "MangaDex releases",
            "media",
            ("api.mangadex.org", "uploads.mangadex.org"),
            "event",
            60,
            "https://mangadex.org/compliance",
            "noncommercial",
            "Data via MangaDex; attribution required, link original chapters and proxy cover images.",
            ("/manga", "/chapter"),
            (
                QuotaWindowPolicy(
                    "mangadex_calls_utc_second", "provider", "second", "http_calls", 5
                ),
                QuotaWindowPolicy(
                    "mangadex_calls_utc_minute", "provider", "minute", "http_calls", 300
                ),
                _unknown(),
            ),
        ),
        checked_on="2026-11-17",
    ),
    replace(
        _spec(
            "apple_books_rss",
            "Apple Books charts",
            "media",
            ("rss.marketingtools.apple.com",),
            "event",
            360,
            "https://www.apple.com/legal/internet-services/itunes/",
            "review",
            "Data via Apple Books; link the original storefront entry.",
            ("/api/v2/{country}/books/", "/api/v2/{country}/audio-books/"),
            (
                QuotaWindowPolicy(
                    "apple_media_calls_utc_minute", "provider", "minute", "http_calls", 20
                ),
                _unknown(),
            ),
        ),
        checked_on="2026-11-17",
    ),
    replace(
        _spec(
            "podcast_rss",
            "Podcast RSS",
            "media",
            ("owner-configured HTTPS feed host", "itunes.apple.com"),
            "event",
            60,
            "https://podcastindex.org/terms",
            "review",
            "Podcast publisher attribution and original episode links are preserved.",
            ("owner-configured / RSS", "/lookup"),
            (
                QuotaWindowPolicy(
                    "podcast_calls_utc_minute", "provider", "minute", "http_calls", 2
                ),
                _unknown(),
            ),
        ),
        checked_on="2026-11-17",
    ),
    replace(
        _spec(
            "wikidata_movies",
            "Wikidata movies",
            "media",
            ("query.wikidata.org",),
            "event",
            360,
            "https://www.wikidata.org/wiki/Wikidata:Licensing",
            "open",
            "Wikidata is CC0; identify the source and link each item.",
            ("/sparql",),
            (
                QuotaWindowPolicy(
                    "wikidata_calls_utc_minute", "provider", "minute", "http_calls", 1
                ),
                _unknown(),
            ),
        ),
        checked_on="2026-11-17",
    ),
    replace(
        _spec(
            "itunes_movies",
            "iTunes movie search",
            "media",
            ("itunes.apple.com",),
            "event",
            360,
            "https://www.apple.com/legal/internet-services/itunes/",
            "review",
            "Data via Apple iTunes; link the original storefront entry.",
            ("/search",),
            (
                QuotaWindowPolicy(
                    "itunes_media_calls_utc_minute", "provider", "minute", "http_calls", 20
                ),
                _unknown(),
            ),
        ),
        checked_on="2026-11-17",
    ),
    replace(
        _spec(
            "letterboxd_rss",
            "Letterboxd diary RSS",
            "media",
            ("letterboxd.com",),
            "event",
            60,
            "https://letterboxd.com/legal/terms-of-use/",
            "review",
            "Data via Letterboxd; link the original film entry.",
            ("/{username}/rss/",),
            (
                QuotaWindowPolicy(
                    "letterboxd_calls_utc_minute", "provider", "minute", "http_calls", 1
                ),
                _unknown(),
            ),
        ),
        checked_on="2026-11-17",
    ),
    _spec(
        "bbc_world",
        "BBC World News",
        "news",
        ("feeds.bbci.co.uk",),
        "news",
        30,
        "https://www.bbc.co.uk/usingthebbc/terms/",
        "review",
        "Display BBC as publisher with a link to the original article; feed-only title, summary and link.",
        ("/news/world/rss.xml",),
        (_day(SECTION_DAILY_CAP), _unknown()),
    ),
    _spec(
        "vnexpress_business",
        "VnExpress Business",
        "news",
        ("vnexpress.net",),
        "news",
        30,
        "https://vnexpress.net/rss",
        "noncommercial",
        "Identify VnExpress clearly and link the original article; feed-only scope.",
        ("/rss/kinh-doanh.rss",),
        (_day(SECTION_DAILY_CAP), _unknown()),
    ),
    _spec(
        "tuoitre_news",
        "Tuổi Trẻ News",
        "news",
        ("tuoitre.vn",),
        "news",
        30,
        "https://tuoitre.vn/rss.htm",
        "noncommercial",
        "Display Tuổi Trẻ clearly as source and link the original article",
        ("/home.rss",),
        (_day(SECTION_DAILY_CAP), _unknown()),
    ),
    _spec(
        "thanhnien_news",
        "Báo Thanh Niên",
        "news",
        ("thanhnien.vn",),
        "news",
        30,
        "https://thanhnien.vn/rss.html",
        "noncommercial",
        'Credit "Báo Thanh Niên" or "Theo Báo Thanh Niên" and link the original article',
        ("/rss/home.rss",),
        (_day(SECTION_DAILY_CAP), _unknown()),
    ),
    _spec(
        "dantri_news",
        "Dân trí News",
        "news",
        ("dantri.com.vn",),
        "news",
        30,
        "https://dantri.com.vn/rss.htm",
        "noncommercial",
        "Credit Dân trí as the source and link the original article; feed-only title, summary and link.",
        ("/rss/home.rss",),
        (_day(SECTION_DAILY_CAP), _unknown()),
    ),
    # Unofficial RSS search endpoint: Google publishes no API contract for it. Interval and pilot cap
    # mirror the other 30-minute news feeds (docs/free-data-provider-policy.md quota table); no official quota is claimed.
    _spec(
        "google_news",
        "Google News RSS",
        "news",
        ("news.google.com",),
        "news",
        30,
        "https://policies.google.com/terms",
        "review",
        "Google News RSS / publisher: show the item's publisher and link to the original article; feed-only title, summary and link.",
        ("/rss/search",),
        (_day(96), _unknown()),
    ),
    # SEC fair access: max 10 requests/second and a declared User-Agent with contact details (owner email in scope).
    _spec(
        "sec_edgar",
        "SEC EDGAR filings",
        "economy",
        ("data.sec.gov",),
        "event",
        360,
        "https://www.sec.gov/privacy.htm#security",
        "open",
        "Source: U.S. SEC EDGAR",
        ("/submissions/CIK##########.json",),
        (_day(96), _unknown()),
    ),
    # OpenAlex works search: data is CC0 (help.openalex.org); basic use needs no key (a free key only raises the
    # daily budget). The 100 req/s cap is far above the one daily send; the local pilot cap stays small.
    _spec(
        "openalex",
        "OpenAlex works search",
        "tech",
        ("api.openalex.org",),
        "event",
        1440,
        "https://help.openalex.org/how-to-use-the-api/api-overview",
        "open",
        "Credit OpenAlex; data is CC0. Link the work's OpenAlex id and DOI; abstracts are rebuilt from the inverted index.",
        ("/works",),
        (_day(8), _unknown()),
    ),
    replace(
        _spec(
            "bluesky",
            "Bluesky public account feed",
            "tech",
            ("public.api.bsky.app", "bsky.app"),
            "news",
            30,
            "https://bsky.social/about/support/tos",
            "review",
            "Attribute Bluesky and link to each original post; Bluesky terms do not establish redistribution rights for post content.",
            ("/xrpc/app.bsky.feed.getAuthorFeed",),
            (_unknown(),),
            execution="supported",
        ),
        checked_on="2026-10-24",
    ),
    replace(
        _spec(
            "telegram_public",
            "Telegram public channel (web preview)",
            "tech",
            ("t.me",),
            "news",
            30,
            "https://telegram.org/tos",
            "review",
            "Attribute the channel and link to each original post; Telegram terms do not establish redistribution rights for post content.",
            ("/s/{username}",),
            (_unknown(),),
            execution="supported",
        ),
        checked_on="2026-11-22",
    ),
    _spec(
        "hn_top",
        "Hacker News top stories",
        "tech",
        ("hacker-news.firebaseio.com",),
        "news",
        60,
        "https://github.com/HackerNews/API",
        "review",
        "Attribute Hacker News plus the item and publisher URL; the API license does not license linked articles.",
        ("/v0/topstories.json", "/v0/item/{id}.json"),
        # Self-imposed politeness (HN's Firebase API publishes no limit). One run = 1 listing + up to 30 item
        # sends (hn_limit hard cap) every 15 min at the fastest schedule: 96 runs x 31 sends/day, and all 31
        # must fit one minute window, else a quota deferral restarts the run and the source starves.
        (
            _day(96 * 31),
            QuotaWindowPolicy("pilot_calls_utc_minute", "provider", "minute", "http_calls", 32),
        ),
    ),
    _spec(
        "gdelt_economy",
        "GDELT economy articles",
        "news",
        ("api.gdeltproject.org",),
        "news",
        60,
        "https://gdeltproject.org/about.html",
        "review",
        "Cite the GDELT Project and the publisher URL; article metadata is not republication permission.",
        ("/api/v2/doc/doc?query=economy&mode=artlist&format=json&maxrecords=5&timespan=24h",),
        (_day(48), _unknown()),
        execution="experimental",
    ),
    _spec(
        "world_bank",
        "World Bank annual GDP (Vietnam)",
        "economy",
        ("api.worldbank.org",),
        "measurement",
        1440,
        "https://datacatalog.worldbank.org/public-licenses",
        "open",
        "Credit the World Bank and the original indicator; retain the dataset license and changed notice.",
        ("/v2/country/VN/indicator/NY.GDP.MKTP.CD?format=json&per_page=3",),
        (_day(32), _unknown()),
    ),
    _spec(
        "frankfurter",
        "Frankfurter USD/VND",
        "economy",
        ("api.frankfurter.dev",),
        "measurement",
        1440,
        "https://frankfurter.dev/license/",
        "open",
        "Credit Frankfurter and contributing providers; the default rate is a blended rate, not a single-bank rate.",
        ("/v2/rate/usd/vnd",),
        (_day(8), _unknown()),
    ),
    _spec(
        "ecb",
        "ECB euro reference rates",
        "economy",
        ("www.ecb.europa.eu",),
        "measurement",
        1440,
        "https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html",
        "open",
        "Credit the ECB; preserve the rate date and identify transformations.",
        ("/stats/eurofxref/eurofxref-daily.xml",),
        (_day(8), _unknown()),
    ),
    _spec(
        "binance",
        "Binance BTCUSDT price",
        "economy",
        ("data-api.binance.vision",),
        "measurement",
        15,
        "https://developers.binance.com/en/docs/products/spot/rest-api",
        "review",
        "Credit Binance; BTCUSDT market, USDT quote. No trading endpoints or keys.",
        ("/api/v3/ticker/price?symbol=BTCUSDT",),
        (
            _day(384, unit="request_weight", cost=2, key="pilot_request_weight_utc_day"),
            QuotaWindowPolicy(
                "pilot_request_weight_utc_minute", "provider", "minute", "request_weight", 4, 2
            ),
            QuotaWindowPolicy(
                "official_request_weight_unknown",
                "ip",
                "minute",
                "request_weight",
                None,
                2,
                "official_unknown",
            ),
        ),
    ),
    # Unofficial VNDirect finfo endpoint (no contract; may change or vanish). Owner-approved 2026-11-04; TCBS rejected
    # because it only answers behind a Cloudflare challenge. Terms URL is the site root (the terms page returns 403
    # to automated fetches, so it could not be verified). Two calls per run (stocks + indices), 6 h floor.
    _spec(
        "vn_stock",
        "Vietnam stocks (VNDirect, unofficial)",
        "economy",
        ("api-finfo.vndirect.com.vn",),
        "measurement",
        360,
        "https://www.vndirect.com.vn/",
        "review",
        "Credit VNDirect; unofficial public endpoint, prices in thousand VND (indices in points), may break without notice.",
        (
            "/v4/stock_prices?q=code:FPT,VCB,HPG,VNM,MWG&sort=date:desc&size=60",
            "/v4/vnmarket_prices?q=code:VNINDEX,VN30&sort=date:desc&size=24",
        ),
        (_day(48), _unknown()),
    ),
    # Unofficial VietnamWorks job-search endpoint (no contract; may change or vanish). Owner-approved 2026-11-04.
    # The terms page is a client-rendered app, so terms were not machine-verified. At most 5 POSTs per run (one per
    # keyword), 6 h floor. Recruiter contact and demographic fields are never stored.
    _spec(
        "vietnamworks_jobs",
        "VietnamWorks jobs (unofficial)",
        "tech",
        ("ms.vietnamworks.com",),
        "news",
        360,
        "https://www.vietnamworks.com/terms-of-use",
        "review",
        "Credit VietnamWorks and link the job posting; unofficial search endpoint, may break without notice.",
        ("/job-search/v1.0/search",),
        (_day(40), _unknown()),
    ),
    # ITviec job pages (schema.org JSON-LD only). Owner decision 2026-11-04: built despite terms section 2.2
    # (retrieval limited to "reasonable personal use"); the owner accepts that terms risk. At most 3 search pages
    # and 10 detail pages per run, 2 s apart, 6 h floor.
    _spec(
        "itviec_jobs",
        "ITviec jobs (crawled)",
        "tech",
        ("itviec.com",),
        "news",
        360,
        "https://itviec.com/blog/terms-and-conditions/",
        "review",
        "Credit ITviec and link the job posting; static JSON-LD only, owner accepted the terms risk 2026-11-04.",
        ("/it-jobs/{keyword}/{city}", "/it-jobs/{slug}"),
        (_day(60), _unknown()),
    ),
    _spec(
        "alternative_me",
        "Alternative.me Fear and Greed",
        "economy",
        ("api.alternative.me",),
        "measurement",
        1440,
        "https://alternative.me/crypto/fear-and-greed-index/",
        "open",
        "Display Alternative.me with a link adjacent to the index, including translated output.",
        ("/fng/?limit=2",),
        (_day(8), _unknown()),
    ),
    _spec(
        "usgs",
        "USGS M4.5+ earthquakes (day)",
        "hazards",
        ("earthquake.usgs.gov",),
        "event",
        60,
        "https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits",
        "open",
        "Credit the U.S. Geological Survey with the event citation; do not fetch detail URLs automatically.",
        ("/earthquakes/feed/v1.0/summary/4.5_day.geojson",),
        (_day(48), _unknown()),
    ),
    # EONET publishes only a visualization/general-information disclaimer, no license or key/rate-limit terms.
    _spec(
        "nasa_eonet",
        "NASA EONET open natural events",
        "hazards",
        ("eonet.gsfc.nasa.gov",),
        "event",
        60,
        "https://eonet.gsfc.nasa.gov/what-is-eonet#disclaimer",
        "review",
        "Credit NASA EONET and the event's listed sources; data is for visualization and general information only, not official extents.",
        ("/api/v3/events?status=open&limit=50",),
        (_day(48), _unknown()),
    ),
    _spec(
        "cisa_kev",
        "CISA Known Exploited Vulnerabilities",
        "security",
        ("raw.githubusercontent.com",),
        "event",
        1440,
        "https://www.cisa.gov/known-exploited-vulnerabilities-catalog",
        "open",
        "Credit CISA Known Exploited Vulnerabilities Catalog (official feed, CC0).",
        ("/cisagov/kev-data/develop/known_exploited_vulnerabilities.json",),
        (_day(8), _unknown()),
    ),
    _spec(
        "coinpaprika",
        "CoinPaprika BTC ticker",
        "economy",
        ("api.coinpaprika.com",),
        "measurement",
        15,
        "https://docs.coinpaprika.com/api-plans",
        "personal",
        "Credit CoinPaprika; free use is personal with no redistribution.",
        ("/v1/tickers/btc-bitcoin",),
        # 96 runs/day at 15 min x up to 5 coins (one call each); the 1 call/second cap is honoured by pacing.
        (
            _day(96 * 5),
            QuotaWindowPolicy("pilot_calls_utc_second", "provider", "second", "http_calls", 1),
            QuotaWindowPolicy(
                "official_calls_per_second",
                "ip",
                "second",
                "http_calls",
                10,
                1,
                "official_documented",
            ),
            QuotaWindowPolicy(
                "official_calls_per_month",
                "ip",
                "month",
                "http_calls",
                20_000,
                1,
                "official_documented",
            ),
        ),
    ),
    _spec(
        "coingecko",
        "CoinGecko crypto prices (Demo)",
        "economy",
        ("api.coingecko.com",),
        "measurement",
        15,
        "https://www.coingecko.com/en/api_terms",
        "review",
        "Show Powered by CoinGecko with a link per the API terms and brand guide.",
        ("/api/v3/simple/price?ids={selected_coin_ids}&vs_currencies=usd",),
        (
            _day(192, kind="credential"),
            QuotaWindowPolicy("pilot_calls_utc_second", "credential", "second", "http_calls", 1),
            QuotaWindowPolicy(
                "official_calls_per_minute",
                "credential",
                "minute",
                "http_calls",
                100,
                1,
                "official_documented",
            ),
            QuotaWindowPolicy(
                "official_call_credits_per_month",
                "credential",
                "month",
                "call_credits",
                10_000,
                1,
                "official_documented",
            ),
        ),
        key=True,
        secret="header",  # noqa: S106  # label/URL/enum value, not a credential
    ),
    # Existing C1 adapter. 12/UTC-day is the conservative pilot cap until the provider reset
    # anchor is evidenced; the official 25/day fact is recorded separately.
    _spec(
        "alpha_vantage",
        "Alpha Vantage daily equities",
        "economy",
        ("www.alphavantage.co",),
        "measurement",
        1440,
        "https://www.alphavantage.co/terms_of_service/",
        "personal",
        "Credit Alpha Vantage with symbol and day; no free realtime entitlement.",
        ("/query?function=TIME_SERIES_DAILY&symbol={symbol}&outputsize=compact",),
        (
            _day(12, kind="credential"),
            QuotaWindowPolicy("pilot_calls_utc_minute", "credential", "minute", "http_calls", 1),
            QuotaWindowPolicy(
                "official_calls_per_day",
                "credential",
                "day",
                "http_calls",
                25,
                1,
                "official_documented",
            ),
        ),
        key=True,
        secret="query",  # noqa: S106  # label/URL/enum value, not a credential
        implemented=True,
    ),
    # ---- Built-in RSS/Atom world-data feeds. Feed-only title/summary/link; the linked page is never fetched.
    # Eligibility follows each publisher's published terms; where a free-reuse statement is not explicit it is review.
    _spec(
        "aljazeera_world",
        "Al Jazeera World News",
        "news",
        ("www.aljazeera.com",),
        "news",
        30,
        "https://www.aljazeera.com/terms-and-conditions",
        "review",
        "Credit Al Jazeera and link the original article; feed-only title, summary and link.",
        ("/xml/rss/all.xml",),
        (_day(96), _unknown()),
    ),
    _spec(
        "npr_news",
        "NPR News",
        "news",
        ("feeds.npr.org",),
        "news",
        30,
        "https://www.npr.org/about-npr/179876898/terms-of-use",
        "noncommercial",
        "Credit NPR and link the original story; RSS is licensed for personal, noncommercial use.",
        ("/1001/rss.xml",),
        (_day(SECTION_DAILY_CAP), _unknown()),
    ),
    _spec(
        "fed_press",
        "Federal Reserve press releases",
        "economy",
        ("www.federalreserve.gov",),
        "news",
        60,
        "https://www.federalreserve.gov/disclaimer.htm",
        "open",
        "Credit the Board of Governors of the Federal Reserve System; link the original release.",
        ("/feeds/press_all.xml",),
        (_day(SECTION_DAILY_CAP), _unknown()),
    ),
    _spec(
        "gdacs_rss",
        "GDACS disaster alerts (RSS)",
        "hazards",
        ("www.gdacs.org",),
        "event",
        60,
        "https://www.gdacs.org/About/overview.aspx",
        "open",
        "Credit GDACS (UN OCHA / European Commission JRC) and link the event page.",
        ("/xml/rss.xml",),
        (_day(48), _unknown()),
    ),
    _spec(
        "lobsters",
        "Lobsters",
        "tech",
        ("lobste.rs",),
        "news",
        60,
        "https://lobste.rs/about",
        "review",
        "Credit Lobsters and link the story; the feed carries submitter links and titles only.",
        ("/rss",),
        (_day(SECTION_DAILY_CAP), _unknown()),
    ),
    # Third-party mirror that scrapes github.com/trending (GitHub has no trending API): unofficial, may lag or break.
    _spec(
        "github_trending",
        "GitHub Trending (mirror)",
        "tech",
        ("mshibanami.github.io",),
        "news",
        360,
        "https://github.com/mshibanami/GitHubTrendingRSS",
        "review",
        "Credit GitHub and the GitHubTrendingRSS mirror; link the repository. Unofficial scrape of github.com/trending.",
        ("/GitHubTrendingRSS/daily/all.xml",),
        (_day(SECTION_DAILY_CAP), _unknown()),
    ),
    # The terms URL is the site's RSS page (its other pages answer 403 to automated fetches, so terms were not read).
    _spec(
        "wwr_jobs",
        "We Work Remotely jobs",
        "tech",
        ("weworkremotely.com",),
        "news",
        360,
        "https://weworkremotely.com/remote-job-rss-feed",
        "review",
        "Credit We Work Remotely and link the job posting; feed-only title, summary and link.",
        ("/categories/remote-programming-jobs.rss",),
        (_day(SECTION_DAILY_CAP), _unknown()),
    ),
    _spec(
        "us_travel_advisories",
        "U.S. travel advisories",
        "hazards",
        ("travel.state.gov",),
        "event",
        360,
        "https://www.state.gov/copyright-information/",
        "open",
        "Credit the U.S. Department of State; advisories are a point-in-time statement, check the original.",
        ("/_res/rss/TAsTWs.xml",),
        (_day(12), _unknown()),
    ),
    _spec(
        "noaa_tsunami",
        "NOAA tsunami bulletins",
        "hazards",
        ("www.tsunami.gov",),
        "event",
        30,
        "https://www.tsunami.gov/",
        "open",
        "Credit the NOAA/NWS Tsunami Warning Centers; this is not an official warning channel.",
        ("/events/xml/PHEBAtom.xml",),
        (_day(96), _unknown()),
    ),
    _spec(
        "jma_eqvol",
        "JMA earthquake and volcano bulletins",
        "hazards",
        ("www.data.jma.go.jp",),
        "event",
        30,
        "https://www.jma.go.jp/jma/en/copyright.html",
        "open",
        "Source: Japan Meteorological Agency; state any processing (feed title and link only).",
        ("/developer/xml/feed/eqvol.xml",),
        (_day(96), _unknown()),
    ),
    # ---- Built-in JSON list/event feeds (providers/json_feed.py). Endpoint shapes probed 2026-10-21.
    _spec(
        "gdacs_events",
        "GDACS Orange/Red disaster events",
        "hazards",
        ("www.gdacs.org",),
        "event",
        60,
        "https://www.gdacs.org/About/overview.aspx",
        "open",
        "Credit GDACS (UN OCHA / European Commission JRC) and link the event report.",
        (
            "/gdacsapi/api/events/geteventlist/SEARCH?eventlist=EQ;TC;FL;VO;WF;DR&alertlevel=Orange;Red",
        ),
        (_day(48), _unknown()),
    ),
    _spec(
        "nws_alerts",
        "NWS severe alerts (US)",
        "hazards",
        ("api.weather.gov",),
        "event",
        15,
        "https://www.weather.gov/disclaimer",
        "open",
        "Credit the U.S. National Weather Service; alerts expire, check the original.",
        ("/alerts/active?status=actual&message_type=alert&severity=Extreme,Severe",),
        (_day(192), _unknown()),
    ),
    _spec(
        "emsc_quakes",
        "EMSC earthquakes (M4+)",
        "hazards",
        ("www.seismicportal.eu",),
        "event",
        30,
        "https://www.emsc-csem.org/",
        "open",
        "Credit EMSC (Euro-Mediterranean Seismological Centre); magnitudes are preliminary.",
        ("/fdsnws/event/1/query?limit=50&format=json&minmag=4",),
        (_day(96), _unknown()),
    ),
    # Launch Library free tier: documented 15 requests/hour, non-commercial. 120-minute interval stays far below it.
    _spec(
        "launch_library",
        "Launch Library upcoming launches",
        "space",
        ("ll.thespacedevs.com",),
        "event",
        360,
        "https://thespacedevs.com/llapi",
        "noncommercial",
        "Credit The Space Devs Launch Library; free tier is non-commercial with a 15 requests/hour limit.",
        ("/2.2.0/launch/upcoming/?limit=10&mode=list",),
        (_day(12), _unknown()),
    ),
    _spec(
        "wikipedia_mostread",
        "Wikipedia most-read articles",
        "tech",
        ("api.wikimedia.org",),
        "news",
        1440,
        "https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use",
        "open",
        "Credit Wikipedia (CC BY-SA 4.0) and link the article; counts are for the previous UTC day.",
        ("/feed/v1/wikipedia/en/featured/{yyyy}/{mm}/{dd}",),
        (_day(4), _unknown()),
    ),
    _spec(
        "wikipedia_top_views",
        "Wikipedia top page views",
        "tech",
        ("wikimedia.org",),
        "news",
        1440,
        "https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use",
        "open",
        "Credit Wikimedia pageviews (CC0 data) and link the article; counts are for the previous UTC day.",
        ("/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/{yyyy}/{mm}/{dd}",),
        (_day(4), _unknown()),
    ),
    _spec(
        "bmkg_quakes",
        "BMKG recent earthquakes (Indonesia)",
        "hazards",
        ("data.bmkg.go.id",),
        "event",
        30,
        "https://data.bmkg.go.id/",
        "open",
        "Source: BMKG (Badan Meteorologi, Klimatologi, dan Geofisika); state any processing.",
        ("/DataMKG/TEWS/gempaterkini.json",),
        (_day(96), _unknown()),
    ),
    _spec(
        "satnogs_observations",
        "SatNOGS recent observations",
        "space",
        ("network.satnogs.org",),
        "event",
        360,
        "https://db.satnogs.org/about",
        "open",
        "Credit SatNOGS Network (CC BY-SA 4.0); station observations are community-contributed.",
        ("/api/observations/?format=json&status=good",),
        (_day(12), _unknown()),
    ),
    # ---- Bespoke keyless mappers. Endpoint shapes probed 2026-10-21.
    _spec(
        "noaa_kp",
        "NOAA planetary Kp index",
        "space",
        ("services.swpc.noaa.gov",),
        "measurement",
        360,
        "https://www.weather.gov/disclaimer",
        "open",
        "Credit NOAA Space Weather Prediction Center; Kp is a 3-hour planetary index (0-9).",
        ("/products/noaa-planetary-k-index.json",),
        (_day(16), _unknown()),
    ),
    _spec(
        "open_er_api",
        "Open ER API daily USD rates",
        "economy",
        ("open.er-api.com",),
        "measurement",
        1440,
        "https://www.exchangerate-api.com/terms",
        "review",
        "Rates By Exchange Rate API (https://www.exchangerate-api.com); attribution is required on the free plan.",
        ("/v6/latest/USD",),
        (_day(4), _unknown()),
    ),
    # OECD documents roughly 20 queries/hour for anonymous SDMX access; one daily send is far below it.
    _spec(
        "oecd_cli",
        "OECD composite leading indicators",
        "economy",
        ("sdmx.oecd.org",),
        "measurement",
        1440,
        "https://www.oecd.org/en/about/terms-conditions.html",
        "open",
        "Source: OECD (CC BY 4.0); cite the dataset OECD.SDD.STES DF_CLI and state any processing.",
        (
            (
                "/public/rest/data/OECD.SDD.STES,DSD_STES@DF_CLI/USA+CHN+JPN+DEU+GBR.M.LI...AA...H"
                "?lastNObservations=24&dimensionAtObservation=AllDimensions"
            ),
        ),
        (_day(4), _unknown()),
    ),
    _spec(
        "iss_position",
        "ISS position",
        "space",
        ("api.wheretheiss.at",),
        "measurement",
        15,
        "https://wheretheiss.at/w/developer",
        "noncommercial",
        "Credit wheretheiss.at; the API is free for non-commercial use with a documented ~1 request/second cap.",
        ("/v1/satellites/25544",),
        (
            _day(96),
            QuotaWindowPolicy("pilot_calls_utc_second", "provider", "second", "http_calls", 1),
        ),
    ),
    _spec(
        "disease_sh",
        "disease.sh global COVID-19 totals",
        "tech",
        ("disease.sh",),
        "measurement",
        1440,
        "https://disease.sh/",
        "review",
        "Credit disease.sh and its upstream sources (Johns Hopkins CSSE and others); totals may be stale.",
        ("/v3/covid-19/all",),
        (_day(4), _unknown()),
    ),
    _spec(
        "ioda",
        "IODA internet outage events",
        "security",
        ("api.ioda.inetintel.cc.gatech.edu",),
        "event",
        60,
        "https://ioda.inetintel.cc.gatech.edu/",
        "noncommercial",
        "Credit the Georgia Tech IODA project; the service is a research tool, terms are not a license grant.",
        ("/v2/outages/events?from={unix}&until={unix}&entityType=country&limit=50",),
        (_day(24), _unknown()),
    ),
    # NVD keyless limit: 5 requests per rolling 30 seconds; one small page every six hours stays far below it.
    _spec(
        "nvd",
        "NVD recent CVEs",
        "security",
        ("services.nvd.nist.gov",),
        "event",
        360,
        "https://nvd.nist.gov/general/FAQ-Sections/General-FAQs",
        "open",
        "Credit the NIST National Vulnerability Database; this product uses NVD data but is not endorsed by NIST.",
        ("/rest/json/cves/2.0?resultsPerPage=20&pubStartDate={start}&pubEndDate={end}",),
        (_day(8), _unknown()),
    ),
    # ---- Keyed free-tier presets (providers/keyed.py). Owner-entered key in connector_rest_credentials; response
    # shapes follow the providers' public API documentation (not probed: no key is available to probe with).
    _spec(
        "fred",
        "FRED series (latest observations)",
        "economy",
        ("api.stlouisfed.org",),
        "measurement",
        1440,
        "https://fred.stlouisfed.org/docs/api/terms_of_use.html",
        "review",
        "This product uses the FRED API but is not endorsed or certified by the Federal Reserve Bank of St. Louis; "
        "some series carry third-party copyright.",
        ("/fred/series/observations?file_type=json&sort_order=desc&limit=5&series_id={series}",),
        (
            _day(8, kind="credential"),
            QuotaWindowPolicy(
                "official_calls_per_minute",
                "credential",
                "minute",
                "http_calls",
                120,
                1,
                "official_documented",
            ),
        ),
        key=True,
        secret="query",  # noqa: S106  # label/URL/enum value, not a credential
    ),
    _spec(
        "finnhub_news",
        "Finnhub market news",
        "news",
        ("finnhub.io",),
        "news",
        60,
        "https://finnhub.io/terms-of-service",
        "personal",
        "Credit Finnhub and link the original article; the free plan is for personal use.",
        ("/api/v1/news?category=general",),
        (
            _day(48, kind="credential"),
            QuotaWindowPolicy(
                "official_calls_per_minute",
                "credential",
                "minute",
                "http_calls",
                60,
                1,
                "official_documented",
            ),
        ),
        key=True,
        secret="header",  # noqa: S106  # label/URL/enum value, not a credential
    ),
    _spec(
        "newsapi",
        "NewsAPI top headlines (US business)",
        "news",
        ("newsapi.org",),
        "news",
        360,
        "https://newsapi.org/terms",
        "review",
        "Credit NewsAPI.org and the article's publisher; the free Developer plan is for development use only.",
        ("/v2/top-headlines?country=us&category=business&pageSize=20",),
        (
            _day(12, kind="credential"),
            QuotaWindowPolicy(
                "official_calls_per_day",
                "credential",
                "day",
                "http_calls",
                100,
                1,
                "official_documented",
            ),
        ),
        key=True,
        secret="header",  # noqa: S106  # label/URL/enum value, not a credential
    ),
    _spec(
        "openweathermap",
        "OpenWeather current conditions",
        "hazards",
        ("api.openweathermap.org",),
        "measurement",
        60,
        "https://openweathermap.org/api",
        "open",
        "Weather data provided by OpenWeather (CC BY-SA 4.0); state any processing.",
        ("/data/2.5/weather?units=metric&lat={lat}&lon={lon}",),
        (
            _day(48, kind="credential"),
            QuotaWindowPolicy(
                "official_calls_per_minute",
                "credential",
                "minute",
                "http_calls",
                60,
                1,
                "official_documented",
            ),
        ),
        key=True,
        secret="query",  # noqa: S106  # label/URL/enum value, not a credential
    ),
    _spec(
        "openaq",
        "OpenAQ latest air quality",
        "hazards",
        ("api.openaq.org",),
        "measurement",
        60,
        "https://docs.openaq.org/about/about",
        "open",
        "Credit OpenAQ and the originating data provider; data licenses vary per provider (mostly CC BY 4.0).",
        ("/v3/locations/{location}/sensors",),
        (_day(48, kind="credential"), _unknown("credential")),
        key=True,
        secret="header",  # noqa: S106  # label/URL/enum value, not a credential
    ),
    _spec(
        "nasa_firms",
        "NASA FIRMS active fires (VIIRS, 24h)",
        "hazards",
        ("firms.modaps.eosdis.nasa.gov",),
        "event",
        360,
        "https://www.earthdata.nasa.gov/engage/open-data-services-software-policies",
        "open",
        "Credit NASA FIRMS (LANCE/EOSDIS); hotspots are detections, not confirmed fires.",
        ("/api/country/csv/{MAP_KEY}/VIIRS_SNPP_NRT/{country}/1",),
        (_day(16, kind="credential"), _unknown("credential")),
        key=True,
        secret="path",  # noqa: S106  # label/URL/enum value, not a credential
    ),
    *MEDIA_KEYED_SPECS,
)

# The built-in expansion (RSS, JSON, bespoke and keyed presets) was checked later than the original 21 presets;
# the date doubles as the terms version the owner acknowledges, so it must not claim the older check.
EXPANSION_CHECKED_ON = "2026-10-21"


def _finalize_spec(spec: FreeProviderSpec) -> FreeProviderSpec:
    """Stamp expansion check date, realtime tier, and default retention window onto a catalog spec.

    Args:
        spec: Raw provider specification entry.

    Returns:
        Frozen FreeProviderSpec with tier and retention defaults populated.
    """
    is_realtime = spec.id in _REALTIME_TIER_IDS
    retention_days = (
        30
        if spec.id == "youtube_api"
        else 14
        if is_realtime
        else (365 if spec.id in _LONG_RETENTION_IDS else 60)
    )
    return replace(
        spec,
        checked_on=EXPANSION_CHECKED_ON if spec.id in _EXPANSION_IDS else spec.checked_on,
        realtime_tier="realtime" if is_realtime else "near_realtime",
        skip_embedding=True,
        default_retention_days=retention_days,
    )


FREE_PROVIDER_SPECS: tuple[FreeProviderSpec, ...] = tuple(
    _finalize_spec(spec) for spec in _SPECS_RAW
)

_BY_ID = {spec.id: spec for spec in FREE_PROVIDER_SPECS}
if len(_BY_ID) != len(FREE_PROVIDER_SPECS):
    raise RuntimeError("invariant violated: duplicate provider id in FREE_PROVIDER_SPECS")


def get_provider_spec(provider_id: str | None) -> FreeProviderSpec | None:
    """Return the exact free-provider spec, or None for any other provider."""
    return _BY_ID.get(provider_id or "")


def get_realtime_tier(
    provider_id: str | None,
    configuration: Mapping[str, object] | None = None,
) -> Literal["realtime", "near_realtime"]:
    """Return the collection tier ('realtime' or 'near_realtime') for a provider.

    Honors an explicit owner-configured ``realtime_mode`` in ``configuration`` when present;
    otherwise high-frequency market tickers, seismic/weather/hazard alerts, and live telemetry
    use the 'realtime' fast lane and periodic feeds/macro series use 'near_realtime'.

    Args:
        provider_id: Provider identifier string or None.
        configuration: Optional persisted source configuration dictionary.

    Returns:
        Literal 'realtime' or 'near_realtime'.
    """
    if isinstance(configuration, Mapping):
        configured = configuration.get("realtime_mode")
        if configured == "realtime":
            return "realtime"
        if configured == "near_realtime":
            return "near_realtime"
    spec = get_provider_spec(provider_id)
    if spec is not None:
        return spec.realtime_tier
    if (provider_id or "").strip().lower() in _REALTIME_TIER_IDS:
        return "realtime"
    return "near_realtime"


def get_default_retention_days(provider_id: str | None) -> int | None:
    """Return the default retention period in days for a provider's historical records.

    Args:
        provider_id: Provider identifier string or None.

    Returns:
        Default retention window in days (14 for high-frequency realtime telemetry,
        365 for macro/filings/advisories, 60 for general news/feeds), or None if unbounded.
    """
    spec = get_provider_spec(provider_id)
    if spec is not None:
        return spec.default_retention_days
    normalized = (provider_id or "").strip().lower()
    if normalized in _REALTIME_TIER_IDS:
        return 14
    if normalized in _LONG_RETENTION_IDS:
        return 365
    return 60


def get_quota_policies(provider_id: str | None) -> tuple[QuotaWindowPolicy, ...]:
    """Return durable send budgets without coupling non-free connectors to terms eligibility.

    Free providers use their documented catalog policy. Google, workspace apps, and explicitly
    configured local services use a conservative instance-wide pilot call budget; no credentials,
    grants, or provider terms are inferred from this policy. Unknown providers fail closed by
    returning no policy and therefore cannot be sent through a quota-enabled adapter.
    """
    if not provider_id:
        return ()
    spec = get_provider_spec(provider_id)
    if spec is not None:
        return spec.quota
    return _NON_FREE_QUOTA_POLICIES.get(provider_id, ())


# Presets the owner can switch on with one toggle: fixed endpoint (no owner scope), no API key, and an
# adapter exists. Derived (not listed) so a new fixed-scope spec is exposed automatically. backends has no
# connector imports, so this module-level import is cycle-safe.
from modules.connectors.backends import FIXED_SCOPE_PROVIDERS  # noqa: E402

# Providers that fetch private/LAN addresses; only the instance operator may create or configure them.
LOCAL_SERVICE_PROVIDERS: frozenset[str] = frozenset({"health", "home_assistant"})
BUILTIN_PROVIDERS: frozenset[str] = frozenset(
    spec.id
    for spec in FREE_PROVIDER_SPECS
    if spec.id in FIXED_SCOPE_PROVIDERS and not spec.key_required and spec.code_implemented
)


# Built-ins whose runners resume from the stored cursor and honour cap/keyword/age options: news and event
# presets (measurement snapshots are always fresh) plus the owner-scoped Google News query source.
# Measurement providers (cisa_kev, usgs, ...) are excluded even when tagged "event": their documents are
# snapshots that never dedup by seen key, so a cursor would only regress or stall.
# Late import: avoids a circular import with the documents contract.
from modules.knowledge.documents.public import MEASUREMENT_PROVIDER_IDS  # noqa: E402

INCREMENTAL_PROVIDERS: frozenset[str] = frozenset(
    spec.id
    for spec in FREE_PROVIDER_SPECS
    if spec.id in BUILTIN_PROVIDERS
    and spec.data_kind in ("news", "event")
    and spec.id not in MEASUREMENT_PROVIDER_IDS
) | {
    "google_news",
    "tmdb_movies",
    "tmdb_tv",
    "trakt_media",
    "omdb_lookup",
    "mal_anime",
    "mal_manga",
    "google_books",
}

# Optional per-sync selection options accepted only by INCREMENTAL_PROVIDERS (see modules.connectors.selection).
COLLECTION_FILTER_FIELDS = frozenset(
    {"max_items", "include_keywords", "exclude_keywords", "max_age_days"}
)


def applicable_options(provider: str) -> list[str]:
    """List the owner options a built-in accepts, in display order.

    ``name`` and the schedule apply to every built-in; the selection filters only to ``INCREMENTAL_PROVIDERS``
    (measurement snapshots are tuned elsewhere); ``feed_sections`` only to providers with a section catalog.
    The registry and ``serialize_source_configuration`` enforce the same allowlists, so the UI can render
    exactly these fields.
    """
    options = ["name", "schedule_interval_minutes"]
    if provider in INCREMENTAL_PROVIDERS:
        options += ["max_items", "include_keywords", "exclude_keywords", "max_age_days"]
    if provider in SECTION_CATALOG:
        options.append("feed_sections")
    options += [name for name in option_fields(provider) if name not in options]
    return options
