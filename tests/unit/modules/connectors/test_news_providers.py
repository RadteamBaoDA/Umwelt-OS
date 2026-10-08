import re
from datetime import UTC, datetime, timedelta

import pytest

from modules.connectors.providers.gdelt import GDELT_TIMEOUT_SECONDS, gdelt_request, map_gdelt
from modules.connectors.providers.macro import ProviderPayloadError
from modules.connectors.providers.news import (
    HN_MAX_ITEMS,
    FeedValidators,
    capture_validators,
    feed_request,
    hn_item_request,
    map_feed,
    map_feed_response,
    map_hn_stories,
    map_news_body,
    parse_hn_top_ids,
)
from tests.unit.modules.connectors.p3_helpers import NOW, fixture, jfixture


def pr(record):
    return record.metadata["provider_record"]


def by_id(records):
    return {r.provider_id: r for r in records}


def test_bbc_fixture_mapping():
    recs = by_id(map_feed("bbc_world", fixture("bbc_world_rss.xml"), NOW))
    assert set(recs) == {"story-42", "https://www.bbc.co.uk/news/articles/c2", "story-43", "story-44"}
    first = recs["story-42"]
    assert first.metadata["title"] == "Market & policy"
    assert first.observed_at == datetime(2026, 10, 7, 8, 0, tzinfo=UTC)
    assert "alert" not in first.content and "<" not in first.content and "Rates hold" in first.content
    assert pr(first)["world_data"] is None
    assert pr(first)["timestamp_basis"] == "provider_published"
    assert pr(first)["source_fields"]["publisher"] == "BBC News"
    assert pr(first)["source_fields"]["canonical_url"].endswith("at_medium=RSS")  # raw origin URL preserved
    assert first.metadata["full_text_available"] is False and first.metadata["content_scope"] == "feed_summary"
    assert recs["https://www.bbc.co.uk/news/articles/c2"].observed_at == datetime(2026, 10, 7, 8, 30, tzinfo=UTC)
    assert "canonical_url" not in pr(recs["story-44"])["source_fields"]  # javascript: link dropped
    assert pr(first)["license_label"]


def test_missing_date_uses_collection_basis_and_version_ignores_clock():
    a = by_id(map_feed("bbc_world", fixture("bbc_world_rss.xml"), NOW))["story-43"]
    b = by_id(map_feed("bbc_world", fixture("bbc_world_rss.xml"), NOW + timedelta(hours=5)))["story-43"]
    assert pr(a)["timestamp_basis"] == "collection" and a.observed_at == NOW
    assert b.observed_at != a.observed_at
    assert a.version == b.version and a.provider_id == b.provider_id


def test_repeated_and_reordered_feed_is_stable():
    xml = fixture("bbc_world_rss.xml").decode()
    items = re.findall(r"<item>.*?</item>", xml, re.DOTALL)
    head, tail = xml.split(items[0])[0], xml.split(items[-1])[1]
    reordered = (head + "".join(reversed(items)) + tail).encode()
    base = map_feed("bbc_world", fixture("bbc_world_rss.xml"), NOW)
    again = map_feed("bbc_world", fixture("bbc_world_rss.xml"), NOW)
    rev = map_feed("bbc_world", reordered, NOW)
    assert [(r.provider_id, r.version) for r in base] == [(r.provider_id, r.version) for r in again]
    assert [(r.provider_id, r.version) for r in base] == [(r.provider_id, r.version) for r in rev]
    assert len({r.provider_id for r in base}) == len(base)


def test_duplicate_guid_in_one_feed_collapses():
    xml = fixture("bbc_world_rss.xml").decode()
    item = re.findall(r"<item>.*?</item>", xml, re.DOTALL)[0]
    doubled = xml.replace("</channel>", item + "</channel>").encode()
    assert len(map_feed("bbc_world", doubled, NOW)) == len(map_feed("bbc_world", fixture("bbc_world_rss.xml"), NOW))


def test_atom_dates_and_modified():
    rec = map_feed("vnexpress_business", fixture("vnexpress_atom.xml"), NOW)[0]
    assert rec.provider_id == "vne-1"
    assert rec.observed_at == datetime(2026, 10, 6, 18, 0, tzinfo=UTC)
    assert pr(rec)["provider_modified_at"].startswith("2026-10-07T03:00:00")
    assert pr(rec)["source_fields"]["summary"] == "Tom tat"


def test_hostile_xml_rejected():
    with pytest.raises(ProviderPayloadError, match="forbidden_xml"):
        map_feed("bbc_world", fixture("news_hostile_doctype.xml"), NOW)


@pytest.mark.parametrize("body", [
    pytest.param("<?xml version='1.0' encoding='UTF-16'?><rss/>".encode("utf-16"), id="utf16"),
    pytest.param(b"<?xml version='1.0' encoding='ISO-8859-1'?><rss/>", id="latin1"),
    pytest.param(b"<html><body>captcha</body></html>", id="html"),
    pytest.param(b"", id="empty"),
    pytest.param(b"<rss><channel></rss>", id="malformed"),
    pytest.param(b"<rss><channel/></rss>", id="no_items"),
    pytest.param(b"x" * (1024 * 1024 + 1), id="oversize"),
])
def test_malformed_or_non_feed_rejected(body):
    with pytest.raises(ProviderPayloadError):
        map_feed("bbc_world", body, NOW)


def test_item_flood_rejected_and_cap_truncates():
    many = "<rss><channel>" + "".join(f"<item><title>t{i}</title><guid>g{i}</guid></item>" for i in range(150)) + "</channel></rss>"
    recs = map_feed("bbc_world", many.encode(), NOW)
    assert len(recs) == 100 and all(pr(r)["coverage"] == "truncated" for r in recs)
    flood = "<rss><channel>" + "<item><title>t</title><guid>g</guid></item>" * 501 + "</channel></rss>"
    with pytest.raises(ProviderPayloadError, match="too_many_items"):
        map_feed("bbc_world", flood.encode(), NOW)


def test_future_and_naive_dates_ignored():
    future = (NOW + timedelta(days=3)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    xml = (f"<rss><channel><item><title>t</title><guid>g</guid><pubDate>{future}</pubDate></item>"
           "<item><title>u</title><guid>h</guid><pubDate>7 Oct 2026 08:00:00 -0000</pubDate></item></channel></rss>")
    assert {pr(r)["timestamp_basis"] for r in map_feed("bbc_world", xml.encode(), NOW)} == {"collection"}


def test_conditional_get_and_304():
    stored = capture_validators("bbc_world", {"ETag": '"abc"', "Last-Modified": "Wed, 07 Oct 2026 08:00:00 GMT"}, "r1")
    assert feed_request("bbc_world", stored, "r1").headers["If-None-Match"] == '"abc"'
    assert "If-None-Match" not in feed_request("bbc_world", stored, "r2").headers  # config revision changed
    assert "If-None-Match" not in feed_request("vnexpress_business", stored, "r1").headers  # other URL
    bad = FeedValidators(stored.url, "r1", "bad\r\nX: y")
    assert "If-None-Match" not in feed_request("bbc_world", bad, "r1").headers
    assert capture_validators("bbc_world", {}) is None
    res = map_feed_response("bbc_world", 304, b"", NOW)
    assert res.not_modified and res.records == ()
    assert len(map_feed_response("bbc_world", 200, fixture("bbc_world_rss.xml"), NOW).records) == 4
    with pytest.raises(ProviderPayloadError):
        map_feed_response("bbc_world", 500, b"", NOW)


def test_feed_request_fixed_hosts():
    assert feed_request("bbc_world").url == "https://feeds.bbci.co.uk/news/world/rss.xml"
    with pytest.raises(ProviderPayloadError):
        feed_request("hn_top")


def test_hn_top_ids_bounded_and_deduped():
    ids = parse_hn_top_ids(fixture("hn_topstories.json"))
    assert len(ids) == HN_MAX_ITEMS and len(set(ids)) == len(ids) and ids[:3] == (41000001, 41000002, 41000003)
    for bad in (b"{}", b"[]", b'["a"]', b"[true]", b"[-1]", b"<html>"):
        with pytest.raises(ProviderPayloadError):
            parse_hn_top_ids(bad)
    assert hn_item_request(41000001).url == "https://hacker-news.firebaseio.com/v0/item/41000001.json"
    with pytest.raises(ProviderPayloadError):
        hn_item_request("1/../x")  # type: ignore[arg-type]


def test_hn_items_skip_removed_and_count():
    ids = (41000001, 41000002, 41000003, 41000004, 41000005, 41000006)
    bodies = {
        41000001: fixture("hn_item_story.json"), 41000002: fixture("hn_item_deleted.json"),
        41000003: fixture("hn_item_dead.json"), 41000004: fixture("hn_item_ask.json"),
        41000005: b"null", 41000006: None,
    }
    res = map_hn_stories(ids, bodies, NOW)
    assert {r.provider_id for r in res.records} == {"hn:41000001", "hn:41000004"}
    assert res.skipped == {"deleted": 1, "dead": 1, "missing": 2}
    story = next(r for r in res.records if r.provider_id == "hn:41000001")
    assert story.metadata["title"] == "Show HN: A thing & more" and pr(story)["world_data"] is None
    assert story.observed_at == datetime.fromtimestamp(1790000000, UTC)
    assert pr(story)["source_fields"]["url"] == "https://example.org/thing"
    assert story.metadata["full_text_available"] is False
    ask = next(r for r in res.records if r.provider_id == "hn:41000004")
    assert "url" not in pr(ask)["source_fields"] and "Body text" in ask.content


def test_hn_version_ignores_score_and_clock():
    raw = fixture("hn_item_story.json")
    bumped = raw.replace(b'"score":321', b'"score":999')
    a = map_hn_stories((41000001,), {41000001: raw}, NOW).records[0]
    b = map_hn_stories((41000001,), {41000001: bumped}, NOW + timedelta(days=1)).records[0]
    assert a.version == b.version
    assert map_hn_stories((41000002,), {41000002: raw}, NOW).skipped == {"id_mismatch": 1}
    with pytest.raises(ProviderPayloadError, match="fanout"):
        map_hn_stories(tuple(range(1, 12)), {}, NOW)


def test_gdelt_request_bounded():
    req = gdelt_request()
    assert req.url == "https://api.gdeltproject.org/api/v2/doc/doc?query=economy&mode=artlist&format=json&maxrecords=5&timespan=24h"
    assert GDELT_TIMEOUT_SECONDS == 8 and "maxrecords=50" in gdelt_request(50).url
    for bad in (0, 51, True, "5"):
        with pytest.raises(ProviderPayloadError):
            gdelt_request(bad)  # type: ignore[arg-type]


def test_gdelt_mapping_dedupes_and_is_stable():
    recs = map_gdelt(jfixture("gdelt_artlist.json"), NOW)
    assert len(recs) == 2  # insecure dropped, duplicate url collapsed
    a = next(r for r in recs if pr(r)["source_fields"]["domain"] == "example.com")
    assert pr(a)["timestamp_basis"] == "collection" and pr(a)["world_data"] is None
    assert pr(a)["source_fields"]["language"] == "English" and a.metadata["full_text_available"] is False
    later = map_gdelt(jfixture("gdelt_artlist.json"), NOW + timedelta(hours=9))
    assert {r.provider_id: r.version for r in recs} == {r.provider_id: r.version for r in later}
    capped = map_gdelt(jfixture("gdelt_artlist.json"), NOW, max_records=1)
    assert len(capped) == 1 and pr(capped[0])["coverage"] == "truncated"


@pytest.mark.parametrize("body", [b"Timespan is too short.", b"", b"[]", b'{"error":"x"}', b"{}", b'{"articles":"no"}'])
def test_gdelt_rejects_error_and_empty_shapes(body):
    with pytest.raises(ProviderPayloadError):
        map_news_body("gdelt_economy", body, NOW)


def test_gdelt_empty_list_is_valid_and_registry_dispatch():
    assert map_news_body("gdelt_economy", b'{"articles":[]}', NOW) == []
    assert len(map_news_body("bbc_world", fixture("bbc_world_rss.xml"), NOW)) == 4
    with pytest.raises(ProviderPayloadError):
        map_news_body("hn_top", b"[]", NOW)
