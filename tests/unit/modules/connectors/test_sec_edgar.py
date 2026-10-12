"""SEC EDGAR preset: scope validation, declared User-Agent, columnar mapping, executor wiring. No network."""

import json
from types import SimpleNamespace

import pytest

from modules.connectors import collection
from modules.connectors.provider_specs import DISPATCHABLE, get_provider_spec
from modules.connectors.providers import rest
from modules.connectors.providers.filings import map_sec_edgar, sec_edgar_request
from modules.connectors.providers.macro import ProviderPayloadError
from tests.unit.modules.connectors.p3_helpers import NOW

CIK = "0000320193"
PAYLOAD = {
    "cik": "320193",
    "filings": {"recent": {
        "accessionNumber": ["0000320193-26-000010", "0000320193-26-000005"],
        "filingDate": ["2026-01-02", "2025-12-30"],
        "form": ["8-K", "4"],
        "primaryDocument": ["a8k.htm", ""],
        "primaryDocDescription": ["CURRENT REPORT", "FORM 4"],
    }},
}


def test_request_declares_user_agent_and_fails_closed_without_scope():
    request = sec_edgar_request(CIK, "me@example.com")
    assert request.url == "https://data.sec.gov/submissions/CIK0000320193.json"
    assert request.headers["User-Agent"] == "Umwelt-OS me@example.com"
    for cik, email in ((CIK, None), (CIK, "bad email@x.com"), ("320193", "me@example.com"), (None, "me@example.com")):
        with pytest.raises(ProviderPayloadError):
            sec_edgar_request(cik, email)


def test_maps_one_record_per_filing_with_archive_url():
    records = {r.provider_id: r for r in map_sec_edgar(PAYLOAD, NOW, CIK)}
    first = records["0000320193-26-000010"]
    assert first.content == "8-K\n\nCURRENT REPORT" and first.observed_at.date().isoformat() == "2026-01-02"
    fields = first.metadata["provider_record"]["source_fields"]
    assert fields["canonical_url"] == "https://www.sec.gov/Archives/edgar/data/320193/000032019326000010/a8k.htm"
    assert fields["form"] == "8-K" and fields["cik"] == CIK
    assert "canonical_url" not in records["0000320193-26-000005"].metadata["provider_record"]["source_fields"]


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(cik="1"),
    lambda p: p["filings"]["recent"].update(form=["8-K"]),
    lambda p: p["filings"]["recent"].update(accessionNumber=["bad", "x"]),
])
def test_rejects_mismatched_or_malformed_payload(mutate):
    payload = json.loads(json.dumps(PAYLOAD))
    mutate(payload)
    with pytest.raises(ProviderPayloadError):
        map_sec_edgar(payload, NOW, CIK)


def test_spec_flags():
    spec = get_provider_spec("sec_edgar")
    assert spec and spec.eligibility == "open" and spec.data_kind == "event" and spec.default_interval_minutes == 360
    assert spec.code_implemented and not spec.runtime_verified and "sec_edgar" in DISPATCHABLE


async def test_executor_sends_once_with_user_agent_and_never_without_contact(monkeypatch):
    sent = []

    class Gate:
        async def fetch_bytes(self, url, **kwargs):
            sent.append((url, kwargs))
            return rest.Fetched(json.dumps(PAYLOAD).encode())

    accepted = []

    async def lease(_run):
        return object()

    async def accept(_run, _lease, records, coverage, _at, *_):
        accepted.append((records, coverage))

    monkeypatch.setattr(collection, "_lease", lease)
    monkeypatch.setattr(collection, "_accept_native", accept)

    def run(config):
        source = SimpleNamespace(provider="sec_edgar", configuration=config)
        return SimpleNamespace(attempt=SimpleNamespace(source=source), gate=Gate(), extras={})

    with pytest.raises(ProviderPayloadError):
        await collection._run_sec_edgar(run({"sec_cik": CIK}))
    assert not sent
    await collection._run_sec_edgar(run({"sec_cik": CIK, "sec_contact_email": "me@example.com"}))
    assert len(sent) == 1 and sent[0][1]["headers"]["User-Agent"] == "Umwelt-OS me@example.com"
    assert len(accepted[0][0]) == 2 and accepted[0][1] == "returned_snapshot"


def test_malformed_row_is_skipped_and_batch_truncated():
    payload = json.loads(json.dumps(PAYLOAD))
    payload["filings"]["recent"]["filingDate"][1] = "not-a-date"
    records = map_sec_edgar(payload, NOW, CIK)
    assert [r.provider_id for r in records] == ["0000320193-26-000010"]
    assert records[0].metadata["provider_record"]["coverage"] == "truncated"


def test_empty_recent_filings_is_incomplete():
    payload = {"cik": "320193", "filings": {"recent": {k: [] for k in PAYLOAD["filings"]["recent"]}}}
    with pytest.raises(ProviderPayloadError) as err:
        map_sec_edgar(payload, NOW, CIK)
    assert err.value.kind == "incomplete"
