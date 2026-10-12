import copy
import json

import pytest

from modules.connectors.providers import security
from modules.connectors.providers.macro import ProviderPayloadError
from modules.connectors.providers.security import map_cisa_kev
from tests.unit.modules.connectors.p3_helpers import NOW, fixture, jfixture, wd


def _body(payload) -> bytes:
    return json.dumps(payload).encode()


def test_cisa_kev_newest_first_one_record_per_cve():
    records = map_cisa_kev(fixture("cisa_kev.json"), NOW)
    assert [r.provider_id for r in records] == ["CVE-2016-3081", "CVE-2015-5477"]
    first = records[0]
    assert first.observed_at.isoformat() == "2026-10-07T00:00:00+00:00" and first.collected_at == NOW
    assert "Apache Struts Command Injection" in first.content and "Apply mitigations" in first.content
    fields = wd(first)["provider_fields"]
    assert fields["vendorProject"] == "Apache" and fields["knownRansomwareCampaignUse"] == "Known"
    assert fields["dueDate"] == "2026-10-28" and fields["dateAdded"] == "2026-10-07"
    assert first.metadata["provider_record"]["coverage"] == "returned_snapshot"


def test_cisa_kev_version_is_stable_and_content_sensitive():
    base = jfixture("cisa_kev.json")
    first = map_cisa_kev(_body(base), NOW)[0]
    assert map_cisa_kev(_body(base), NOW.replace(hour=9))[0].version == first.version
    changed = copy.deepcopy(base)
    changed["vulnerabilities"][1]["dueDate"] = "2026-11-01"
    assert map_cisa_kev(_body(changed), NOW)[0].version != first.version


def test_cisa_kev_bounds_to_newest_and_marks_truncated(monkeypatch):
    monkeypatch.setattr(security, "_MAX_RECORDS", 1)
    records = map_cisa_kev(fixture("cisa_kev.json"), NOW)
    assert [r.provider_id for r in records] == ["CVE-2016-3081"]
    assert records[0].metadata["provider_record"]["coverage"] == "truncated"


def _mutated(fn):
    payload = copy.deepcopy(jfixture("cisa_kev.json"))
    fn(payload)
    return _body(payload)


@pytest.mark.parametrize("body", [
    b"", b"<html>", b"[]", b"{}", _body({"vulnerabilities": {}}),
    _mutated(lambda p: p.update(count=5)),
    _mutated(lambda p: p["vulnerabilities"][0].update(cveID="not-a-cve")),
    _mutated(lambda p: p["vulnerabilities"][1].update(cveID="CVE-2015-5477")),
    _mutated(lambda p: p["vulnerabilities"][0].update(dateAdded="yesterday")),
    _mutated(lambda p: p["vulnerabilities"][0].update(dateAdded="2099-01-01")),
    _mutated(lambda p: p["vulnerabilities"][0].update(vulnerabilityName="")),
    _mutated(lambda p: p["vulnerabilities"][0].update(dueDate="soon")),
])
def test_cisa_kev_hostile_payloads_raise(body):
    with pytest.raises(ProviderPayloadError):
        map_cisa_kev(body, NOW)


def test_cisa_kev_oversized_body_rejected():
    with pytest.raises(ProviderPayloadError):
        map_cisa_kev(b" " * (security.KEV_MAX_BYTES + 1), NOW)
