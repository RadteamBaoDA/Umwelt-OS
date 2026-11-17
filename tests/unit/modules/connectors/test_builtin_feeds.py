"""Built-in RSS/Atom world-data presets: one fixture-backed mapper test per feed family (no network)."""

import pytest

from modules.connectors.backends import PROVIDER_SOURCE_TYPES
from modules.connectors.providers import news
from modules.knowledge.documents.schemas import BUILTIN_FEED_PROVIDER_IDS
from tests.unit.modules.connectors.p3_helpers import NOW, fixture

NEW_FEEDS = (
    "aljazeera_world",
    "npr_news",
    "fed_press",
    "gdacs_rss",
    "lobsters",
    "us_travel_advisories",
    "noaa_tsunami",
    "jma_eqvol",
)


def test_new_feed_presets_are_registered_everywhere():
    assert set(NEW_FEEDS) <= set(BUILTIN_FEED_PROVIDER_IDS)
    for provider in NEW_FEEDS:
        assert news.FEED_URLS[provider].startswith("https://") and provider in news.PUBLISHERS
        assert PROVIDER_SOURCE_TYPES[provider] == "rss"


def test_rss_family_maps_titles_links_and_publisher():
    records = news.map_feed("fed_press", fixture("fed_press_rss.xml"), NOW)
    assert len(records) == 2
    first = max(records, key=lambda r: r.observed_at)
    fields = first.metadata["provider_record"]["source_fields"]
    assert fields["publisher"] == "Federal Reserve Board" and fields["canonical_url"].startswith(
        "https://www.federalreserve.gov/"
    )
    assert first.metadata["title"] == "Federal Reserve issues FOMC statement"
    assert first.metadata["provider_record"]["license_label"]


def test_atom_family_uses_entry_id_and_updated_when_no_published():
    records = news.map_feed("jma_eqvol", fixture("jma_eqvol_atom.xml"), NOW)
    assert {r.provider_id for r in records} == {
        "https://www.data.jma.go.jp/developer/xml/data/20261007080049_0_VFVO53_010000.xml",
        "https://www.data.jma.go.jp/developer/xml/data/20261007070000_0_VXSE53_010000.xml",
    }
    assert all(
        r.metadata["provider_record"]["timestamp_basis"] == "provider_modified" for r in records
    )


@pytest.mark.parametrize("provider", NEW_FEEDS)
def test_every_new_feed_has_an_adapter_request(provider):
    assert news.news_adapters()[provider].request().url == news.FEED_URLS[provider]


def test_github_trending_and_wwr_jobs_map_titles_links_and_publisher():
    """The two table-driven RSS built-ins reuse the feed mapper: title, summary, link, publisher label."""
    trending = news.map_feed("github_trending", fixture("github_trending_rss.xml"), NOW)
    assert {r.metadata["title"] for r in trending} == {
        "example-org/agent-toolkit",
        "example-org/fast-parser",
    }
    fields = trending[0].metadata["provider_record"]["source_fields"]
    assert fields["publisher"] == "GitHub Trending" and fields["canonical_url"].startswith(
        "https://github.com/example-org/"
    )
    (job,) = news.map_feed("wwr_jobs", fixture("wwr_jobs_rss.xml"), NOW)
    job_fields = job.metadata["provider_record"]["source_fields"]
    assert job_fields["publisher"] == "We Work Remotely" and job_fields["canonical_url"].startswith(
        "https://weworkremotely.com/remote-jobs/"
    )
    assert "Senior Backend Engineer" in job.metadata["title"]


def test_github_trending_and_wwr_jobs_sections_resolve_to_declared_hosts():
    """Non-default sections build URLs on the preset host; the default equals the legacy single feed URL."""
    assert news.section_urls(
        "github_trending", {"feed_sections": ["weekly-typescript", "daily-all"]}
    ) == [
        (
            "weekly-typescript",
            "https://mshibanami.github.io/GitHubTrendingRSS/weekly/typescript.xml",
        ),
        ("daily-all", news.FEED_URLS["github_trending"]),
    ]
    assert news.section_urls("wwr_jobs", {"feed_sections": ["devops-sysadmin"]}) == [
        ("devops-sysadmin", "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss")
    ]
