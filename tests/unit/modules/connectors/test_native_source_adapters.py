"""Mock-only contract tests for the native datasource adapters added in this rollout."""

import base64
import json
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from modules.connectors.providers import (
    google_api,
    local_services,
    manual_finance,
    rest,
    social_feeds,
    social_networks,
    workspace_apps,
)


def _fetched(value: object) -> rest.Fetched:
    return rest.Fetched(json.dumps(value).encode())


@pytest.mark.asyncio
async def test_gmail_snapshot_skips_attachments_and_commits_history_cursor(monkeypatch: pytest.MonkeyPatch):
    collected = datetime(2026, 10, 24, tzinfo=UTC)
    calls: list[str] = []

    async def fetch(url: str, **_kwargs: object) -> rest.Fetched:
        calls.append(url)
        if url.endswith("/profile"):
            return _fetched({"historyId": "100"})
        if "/messages?" in url:
            return _fetched({"messages": [{"id": "message-1"}]})
        assert "/messages/message-1?" in url
        return _fetched({
            "id": "message-1", "threadId": "thread-1", "historyId": "101", "internalDate": "1792800000000",
            "labelIds": ["INBOX"], "snippet": "fallback",
            "payload": {"headers": [{"name": "Subject", "value": "Hello"}], "parts": [
                {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"body").decode()}},
                {"mimeType": "text/plain", "filename": "secret.txt", "body": {"attachmentId": "a1", "data": "c2VjcmV0"}},
            ]},
        })

    async def send(url: str, **_kwargs: object) -> rest.Fetched:
        return await fetch(url)

    monkeypatch.setattr(rest, "fetch_bounded", send)

    async def before_request() -> None:
        return None

    result = await google_api.collect_gmail("token", ("INBOX",), before_request=before_request, collected_at=collected)
    assert len(result.records) == 1
    assert result.records[0].content == "Hello\n\nbody"
    assert "secret" not in result.records[0].content
    assert result.records[0].metadata["provider_record"]["identity"] == "message-1"
    cursor = json.loads(result.cursor_after)
    assert cursor["phase"] == "history" and cursor["history_id"] == "100"
    assert "after:" in parse_qs(urlsplit(calls[1]).query)["q"][0]


@pytest.mark.asyncio
async def test_gmail_history_advances_only_from_provider_history_id(monkeypatch: pytest.MonkeyPatch):
    collected = datetime(2026, 10, 24, tzinfo=UTC)
    urls: list[str] = []

    async def send(url: str, **_kwargs: object) -> rest.Fetched:
        urls.append(url)
        if "/history?" in url:
            return _fetched({"historyId": "108", "history": [{"messagesAdded": [{"message": {"id": "m2"}}]}]})
        return _fetched({
            "id": "m2", "threadId": "t2", "historyId": "108", "internalDate": "1792800000000",
            "labelIds": ["INBOX"], "snippet": "new", "payload": {},
        })

    async def before_request() -> None:
        return None

    monkeypatch.setattr(rest, "fetch_bounded", send)
    previous = json.dumps({"v": 2, "provider": "gmail", "labels": ["INBOX"], "phase": "history",
                           "history_id": "100", "history_page_token": None, "history_offset": 0})
    result = await google_api.collect_gmail("token", ("INBOX",), before_request=before_request,
                                            cursor=previous, collected_at=collected)
    assert len(result.records) == 1 and result.records[0].provider_id == "m2"
    assert json.loads(result.cursor_after)["history_id"] == "108"
    assert parse_qs(urlsplit(urls[0]).query)["startHistoryId"] == ["100"]


@pytest.mark.asyncio
async def test_calendar_retains_cancellation_recurrence_and_calendar_scoped_sync_token(monkeypatch: pytest.MonkeyPatch):
    async def send(_url: str, **_kwargs: object) -> rest.Fetched:
        return _fetched({"items": [{"id": "event-1", "status": "cancelled", "summary": "Moved",
                                      "recurrence": ["RRULE:FREQ=WEEKLY"], "updated": "2026-10-23T12:00:00Z"}],
                         "nextSyncToken": "calendar-token"})

    async def before_request() -> None:
        return None

    monkeypatch.setattr(rest, "fetch_bounded", send)
    result = await google_api.collect_calendar("token", ("calendar-1",), before_request=before_request,
                                               collected_at=datetime(2026, 10, 24, tzinfo=UTC))
    assert len(result.records) == 1
    record = result.records[0]
    assert record.provider_id == "calendar-1/event-1"
    assert record.metadata["status"] == "cancelled"
    assert record.metadata["recurrence"] == ["RRULE:FREQ=WEEKLY"]
    assert json.loads(result.cursor_after)["tokens"] == ["calendar-token"]


@pytest.mark.asyncio
async def test_calendar_stops_after_each_selected_calendar_completes_its_sweep(monkeypatch: pytest.MonkeyPatch):
    requests: list[str] = []

    async def send(url: str, **_kwargs: object) -> rest.Fetched:
        requests.append(url)
        calendar_id = "calendar-1" if "/calendar-1/" in url else "calendar-2"
        return _fetched({
            "items": [{"id": f"event-{calendar_id}", "summary": calendar_id,
                        "start": {"dateTime": "2026-10-23T10:00:00Z"}}],
            "nextSyncToken": f"sync-{calendar_id}",
        })

    async def before_request() -> None:
        return None

    monkeypatch.setattr(rest, "fetch_bounded", send)
    result = await google_api.collect_calendar(
        "token", ("calendar-1", "calendar-2"), before_request=before_request,
        collected_at=datetime(2026, 10, 24, tzinfo=UTC))

    assert len(requests) == 2
    assert {record.provider_id for record in result.records} == {
        "calendar-1/event-calendar-1", "calendar-2/event-calendar-2",
    }
    cursor = json.loads(result.cursor_after)
    assert result.coverage == "returned_snapshot"
    assert cursor["calendar_index"] == 0
    assert cursor["tokens"] == ["sync-calendar-1", "sync-calendar-2"]


@pytest.mark.asyncio
async def test_drive_exports_google_docs_and_advances_changes_token_with_snapshot(monkeypatch: pytest.MonkeyPatch):
    urls: list[str] = []

    async def send(url: str, **_kwargs: object) -> rest.Fetched:
        urls.append(url)
        if "/changes/startPageToken" in url:
            return _fetched({"startPageToken": "start-1"})
        if "/files/file-1?" in url:
            return _fetched({"id": "file-1", "name": "Notes", "mimeType": "application/vnd.google-apps.document",
                             "modifiedTime": "2026-10-23T08:00:00Z", "version": "7"})
        if "/export?" in url:
            return rest.Fetched(b"document text")
        assert "/changes?" in url
        return _fetched({"changes": [], "newStartPageToken": "start-2"})

    async def before_request() -> None:
        return None

    monkeypatch.setattr(rest, "fetch_bounded", send)
    result = await google_api.collect_drive("token", ("file-1",), before_request=before_request,
                                            collected_at=datetime(2026, 10, 24, tzinfo=UTC))
    assert len(result.records) == 1 and result.records[0].content == "document text"
    assert result.records[0].metadata["mime_type"] == "text/plain"
    assert json.loads(result.cursor_after)["change_token"] == "start-2"
    assert any("/export?mimeType=text%2Fplain" in url for url in urls)


def test_finance_csv_keeps_decimal_and_rejects_implicit_fields():
    parsed = manual_finance.parse_finance_csv(
        b"account_id,transaction_id,date,amount,currency,description\n"
        b"checking,txn-1,2026-10-23,-12.3400,USD,Coffee\n")
    transaction = parsed.transactions[0]
    assert transaction.amount == Decimal("-12.3400")
    assert transaction.amount.as_tuple().exponent == -4
    assert parsed.local_only is True
    with pytest.raises(ValueError, match="exactly"):
        manual_finance.parse_finance_csv(b"date,amount\n2026-10-23,12\n")
    with pytest.raises(ValueError, match="finite"):
        manual_finance.parse_finance_csv(
            b"account_id,transaction_id,date,amount,currency,description\n"
            b"a,t,2026-10-23,NaN,USD,x\n")


def test_finance_ofx_uses_explicit_ofxtools_account_currency_and_decimal_fields():
    content = b"""OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX><SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS><DTSERVER>20261024000000[-5:EST]<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO</STATUS><STMTRS><CURDEF>USD<BANKACCTFROM><BANKID>123<ACCTID>checking<ACCTTYPE>CHECKING</BANKACCTFROM><BANKTRANLIST><DTSTART>20261001000000[-5:EST]<DTEND>20261024000000[-5:EST]<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261023080000[-5:EST]<TRNAMT>-12.3400<FITID>txn-1<NAME>Coffee</STMTTRN></BANKTRANLIST><LEDGERBAL><BALAMT>-12.3400<DTASOF>20261024000000[-5:EST]</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"""

    imported = manual_finance.parse_finance_ofx(content)
    transaction = imported.transactions[0]
    assert transaction.account_id == "checking"
    assert transaction.transaction_id == "txn-1"
    assert transaction.amount == Decimal("-12.3400")
    assert transaction.amount.as_tuple().exponent == -4
    assert transaction.currency == "USD"
    assert imported.local_only is True


def test_finance_ofx2_xml_uses_statement_account_and_currency():
    content = b'''<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<?OFX OFXHEADER="200" VERSION="202" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>
<OFX><SIGNONMSGSRSV1><SONRS><STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS><DTSERVER>20261024000000[-5:EST]</DTSERVER><LANGUAGE>ENG</LANGUAGE></SONRS></SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><TRNUID>1</TRNUID><STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS><STMTRS><CURDEF>USD</CURDEF><BANKACCTFROM><BANKID>123</BANKID><ACCTID>checking</ACCTID><ACCTTYPE>CHECKING</ACCTTYPE></BANKACCTFROM><BANKTRANLIST><DTSTART>20261001000000[-5:EST]</DTSTART><DTEND>20261024000000[-5:EST]</DTEND><STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20261023080000[-5:EST]</DTPOSTED><TRNAMT>-12.3400</TRNAMT><FITID>txn-2</FITID><NAME>Lunch</NAME></STMTTRN></BANKTRANLIST><LEDGERBAL><BALAMT>-12.3400</BALAMT><DTASOF>20261024000000[-5:EST]</DTASOF></LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>'''

    transaction = manual_finance.parse_finance_ofx(content).transactions[0]
    assert (transaction.account_id, transaction.transaction_id, transaction.currency) == ("checking", "txn-2", "USD")
    assert transaction.amount == Decimal("-12.3400")


def test_fhir_observation_is_patient_scoped_and_preserves_value_unit_time():
    collected = datetime(2026, 10, 24, tzinfo=UTC)
    resource = {"resourceType": "Observation", "id": "obs-1", "subject": {"reference": "Patient/p1"},
                "valueQuantity": {"value": Decimal("36.500"), "unit": "Cel"},
                "effectiveDateTime": "2026-10-23T08:30:00+07:00", "meta": {"versionId": "4"}}
    record = local_services.map_fhir_observation(resource, patient_id="p1", collected_at=collected)
    assert record is not None
    assert record.content == "36.500 Cel"
    assert record.metadata["unit"] == "Cel" and record.metadata["observation_id"] == "obs-1"
    assert record.observed_at == datetime(2026, 10, 23, 1, 30, tzinfo=UTC)
    assert local_services.map_fhir_observation(resource, patient_id="p2", collected_at=collected) is None


@pytest.mark.asyncio
async def test_local_service_transport_pins_private_dns_and_blocks_redirects(monkeypatch: pytest.MonkeyPatch):
    original_client = httpx.AsyncClient
    redirects = False
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if redirects:
            return httpx.Response(302, stream=httpx.ByteStream(b""),
                                  headers={"Location": "http://public.example/steal-token"})
        return httpx.Response(200, stream=httpx.ByteStream(b"ok"))

    monkeypatch.setattr(local_services.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (local_services.socket.AF_INET, local_services.socket.SOCK_STREAM, 6, "", ("10.0.0.2", 8123)),
    ])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond), **kwargs))
    before_send = AsyncMock()

    body = await local_services.fetch_configured_origin(
        "http://homeassistant.local:8123", "http://homeassistant.local:8123/api/states/sensor.temp",
        before_send=before_send, bearer_token="secret")
    assert body == b"ok"
    assert requests[0].url.host == "10.0.0.2"
    assert requests[0].headers["host"] == "homeassistant.local:8123"
    assert requests[0].headers["authorization"] == "Bearer secret"
    before_send.assert_awaited_once()

    redirects = True
    with pytest.raises(local_services.LocalServiceError, match="redirect_blocked"):
        await local_services.fetch_configured_origin(
            "http://homeassistant.local:8123", "http://homeassistant.local:8123/api/states/sensor.temp",
            before_send=before_send, bearer_token="secret")
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_home_assistant_snapshot_resumes_with_scope_bound_cursor(monkeypatch: pytest.MonkeyPatch):
    requested: list[str] = []

    async def fetch(_origin: str, url: str, **_kwargs: object) -> bytes:
        requested.append(url)
        entity_id = url.rsplit("/", 1)[-1]
        return json.dumps({"entity_id": entity_id, "state": "21.5", "last_updated": "2026-10-23T08:00:00Z",
                           "attributes": {"unit_of_measurement": "°C", "latitude": 21.0, "longitude": 105.8}}).encode()

    async def before_send() -> None:
        return None

    monkeypatch.setattr(local_services, "fetch_configured_origin", fetch)
    entities = tuple(f"sensor.temp_{index}" for index in range(26))
    first = await local_services.collect_home_assistant_snapshot(
        "http://homeassistant.local:8123", entities, bearer_token="token", before_send=before_send,
        collected_at=datetime(2026, 10, 24, tzinfo=UTC))
    assert first.coverage == "truncated" and len(first.records) == 25
    assert first.records[0].metadata["latitude"] == "21.0"
    second = await local_services.collect_home_assistant_snapshot(
        "http://homeassistant.local:8123", entities, bearer_token="token", before_send=before_send,
        cursor=first.cursor_after, collected_at=datetime(2026, 10, 24, tzinfo=UTC))
    assert second.coverage == "returned_snapshot" and len(second.records) == 1
    assert len(requested) == 26


@pytest.mark.asyncio
async def test_reddit_uses_read_endpoint_and_returns_source_link_cursor():
    urls: list[str] = []

    async def fetch(url: str, **_kwargs: object) -> rest.Fetched:
        urls.append(url)
        return _fetched({"data": {"children": [{"data": {
            "id": "abc", "permalink": "/r/python/comments/abc/title/", "title": "Post",
            "selftext": "Body", "author": "alice", "created_utc": 1792800000,
        }}], "after": "t3_next"}})

    result = await social_networks.collect_reddit_subreddit(
        "python", "token", cursor=None, collected_at=datetime(2026, 10, 24, tzinfo=UTC), fetch_bytes=fetch)
    assert "oauth.reddit.com/r/python/new.json" in urls[0]
    assert len(result.records) == 1 and result.records[0].content == "Post\n\nBody"
    assert result.records[0].metadata["provider_record"]["source_fields"]["canonical_url"].startswith("https://www.reddit.com/")
    assert json.loads(result.cursor_after)["page"] == "t3_next"


@pytest.mark.asyncio
async def test_bluesky_and_mastodon_keep_public_post_provenance_and_skip_private_posts():
    collected = datetime(2026, 10, 24, tzinfo=UTC)
    bluesky_queries: list[dict[str, list[str]]] = []

    async def bluesky_fetch(url: str) -> rest.Fetched:
        query = parse_qs(urlsplit(url).query)
        bluesky_queries.append(query)
        post_id = "p2" if query.get("cursor") == ["cursor-page-2"] else "p1"
        payload = {"feed": [{"post": {"uri": f"at://did:plc:abc/app.bsky.feed.post/{post_id}", "cid": "cid-1",
                                         "record": {"text": "hello", "createdAt": "2026-10-23T08:00:00Z"},
                                         "author": {"handle": "alice.bsky.social"}}}]}
        if not query.get("cursor"):
            payload["cursor"] = "cursor-page-2"
        return _fetched(payload)

    bluesky = await social_feeds.collect_bluesky_profile("alice.bsky.social", fetch_bytes=bluesky_fetch,
                                                         collected_at=collected)
    assert bluesky.records[0].content == "hello"
    assert bluesky.records[0].metadata["provider_record"]["identity"].startswith("at://")
    assert bluesky.records[0].metadata["canonical_url"].startswith("https://bsky.app/")
    assert bluesky.coverage == "truncated" and bluesky.cursor_after
    next_page = await social_feeds.collect_bluesky_profile(
        "alice.bsky.social", fetch_bytes=bluesky_fetch, collected_at=collected, cursor=bluesky.cursor_after)
    assert bluesky_queries[1]["cursor"] == ["cursor-page-2"]
    assert next_page.records[0].provider_id.endswith("/p2")
    assert next_page.coverage == "returned_snapshot" and next_page.cursor_after is None

    async def mastodon_fetch(url: str, **_kwargs: object) -> rest.Fetched:
        assert "timelines/tag/osint" in url
        return _fetched([
            {"id": "100", "visibility": "public", "url": "https://mastodon.social/@alice/100",
             "created_at": "2026-10-23T08:00:00Z", "content": "<p>public post</p>",
             "account": {"acct": "alice"}},
            {"id": "101", "visibility": "followers", "url": "https://mastodon.social/@alice/101",
             "created_at": "2026-10-23T09:00:00Z", "content": "private", "account": {"acct": "alice"}},
        ])

    mastodon = await social_networks.collect_mastodon_scope(
        "https://mastodon.social", account=None, hashtag="osint", access_token=None, cursor=None,
        collected_at=collected, fetch_bytes=mastodon_fetch)
    assert [record.content for record in mastodon.records] == ["public post"]
    assert mastodon.records[0].metadata["provider_record"]["source_fields"]["canonical_url"] == "https://mastodon.social/@alice/100"


@pytest.mark.asyncio
async def test_x_recent_query_uses_bearer_and_persists_api_cursor():
    requests: list[tuple[str, dict[str, str]]] = []

    async def fetch(url: str, **kwargs: object) -> rest.Fetched:
        requests.append((url, kwargs["headers"]))
        return _fetched({"data": [{"id": "123", "text": "public", "created_at": "2026-10-23T08:00:00Z",
                                   "author_id": "42", "lang": "en"}], "meta": {"next_token": "next-1"}})

    result = await social_networks.collect_x_scope(account=None, query="climate", access_token="bearer",
                                                   cursor=None, collected_at=datetime(2026, 10, 24, tzinfo=UTC),
                                                   fetch_bytes=fetch)
    assert "search/recent" in requests[0][0] and requests[0][1]["Authorization"] == "Bearer bearer"
    assert result.records[0].content == "public"
    assert json.loads(result.cursor_after)["page"] == "next-1"


@pytest.mark.asyncio
async def test_notion_pins_api_version_and_slack_excludes_thread_replies():
    notion_headers: list[dict[str, str]] = []

    async def notion_fetch(url: str, **kwargs: object) -> rest.Fetched:
        notion_headers.append(kwargs["headers"])
        if "/pages/" in url:
            return _fetched({"object": "page", "id": "a1234567-b123-c123-d123-e123456789ab",
                             "last_edited_time": "2026-10-23T08:00:00Z", "url": "https://www.notion.so/a",
                             "properties": {"Name": {"title": [{"plain_text": "Note"}]}}})
        return _fetched({"results": [{"id": "block-1", "type": "paragraph", "has_children": False,
                                      "paragraph": {"rich_text": [{"plain_text": "Content"}]}}], "has_more": False})

    notion = await workspace_apps.collect_notion_pages(
        "secret", workspace_apps.NotionScope(("a1234567-b123-c123-d123-e123456789ab",)),
        fetch_bytes=notion_fetch, collected_at=datetime(2026, 10, 24, tzinfo=UTC))
    assert notion.records[0].content == "Note\nContent"
    assert all(header["Notion-Version"] == "2026-03-11" for header in notion_headers)

    async def slack_fetch(url: str, **_kwargs: object) -> rest.Fetched:
        if "conversations.info" in url:
            return _fetched({"ok": True, "channel": {"id": "C012ABCDEF", "is_im": False, "is_mpim": False,
                                                          "is_private": False, "is_channel": True, "is_member": True,
                                                          "is_archived": False}})
        return _fetched({"messages": [{"ts": "1792800000.000001", "text": "root", "channel": "C012ABCDEF"},
                                      {"ts": "1792800001.000001", "thread_ts": "1792800000.000001", "text": "reply"}],
                         "has_more": False, "response_metadata": {"next_cursor": ""}})

    slack = await workspace_apps.collect_slack_channel_messages(
        "xoxb-test", workspace_apps.SlackScope(("C012ABCDEF",)), fetch_bytes=slack_fetch,
        collected_at=datetime(2026, 10, 24, tzinfo=UTC))
    assert [record.content for record in slack.records] == ["root"]

