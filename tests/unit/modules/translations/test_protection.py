import pytest

from modules.translations import protection as p


def test_missing_or_duplicate_amount_fails_closed():
    protected = {"MARK_A": "1,250 USD"}
    p.require_protected_markers("Giá MARK_A", protected)
    with pytest.raises(ValueError, match="protected_value"):
        p.require_protected_markers("Giá 1.250 VND", protected)
    with pytest.raises(ValueError, match="protected_value"):
        p.require_protected_markers("MARK_A và MARK_A", protected)


def test_values_roundtrip_exactly():
    text = (
        "Revenue rose +12.5% to 1,250 USD on 2026-10-08 [3], see https://a.io/x?y=1 "
        "and `code 42` (AAPL) $TSLA [link](https://b.io/p)"
    )
    masked, protected = p.protect_text(text)
    literals = (
        "+12.5%",
        "1,250 USD",
        "2026-10-08",
        "[3]",
        "https://a.io/x?y=1",
        "`code 42`",
        "(AAPL)",
        "$TSLA",
        "https://b.io/p",
    )
    for literal in literals:
        assert literal in protected.values()
        assert literal not in masked
    assert p.restore_text(masked, protected) == text


def test_reordered_values_rejected():
    _, protected = p.protect_text("A 10 and B 20")
    a, b = protected
    with pytest.raises(ValueError, match="order"):
        p.restore_text(f"B {b} and A {a}", protected)


def test_unknown_marker_rejected():
    masked, protected = p.protect_text("x 5")
    with pytest.raises(ValueError, match="unknown"):
        p.restore_text(masked + " @@Pdeadbeef_9@@", protected)


def test_invented_tokens_rejected():
    p.reject_new_tokens("pay 5 USD", "trả 5 USD")
    for bad in ("trả 6 USD", "trả 5 USD [1]", "xem https://evil.io", "mua $ABC"):
        with pytest.raises(ValueError, match="new_token"):
            p.reject_new_tokens("pay 5 USD", bad)


def test_month_name_may_become_month_number():
    src = "Minutes of the FOMC, October 27-28, 2026"
    for ok in ("Biên bản FOMC, ngày 27-28 tháng 10 năm 2026", "Biên bản FOMC, 27-28/10/2026"):
        p.reject_new_tokens(src, ok)
    p.reject_new_tokens("due Sept. 5", "hạn 5/9")
    for bad in ("Biên bản FOMC, ngày 27-28 tháng 11 năm 2026", "Biên bản, 27-29/10/2026"):
        with pytest.raises(ValueError, match="new_token"):
            p.reject_new_tokens(src, bad)
    with pytest.raises(
        ValueError, match="new_token"
    ):  # no month name in the source: no new month number
        p.reject_new_tokens("pay 5 USD", "trả 5 USD tháng 10")
    bypasses = [
        ("rose 5 points", "giảm -5 điểm"),  # sign flip
        ("Marketing team", "đội 3"),  # month-like word
        ("Junior staff", "nhân viên 6"),
        ("see https://x.io/a/42 now", "xem https://x.io/a/42 nay 42"),  # digit from a URL
    ]
    for src, bad in bypasses:
        with pytest.raises(ValueError, match="new_token"):
            p.reject_new_tokens(src, bad)
    p.reject_new_tokens("due Sept. 5 or March 3", "hạn 5/9 hoặc 3/3")
    not_dates = [
        ("Q3 rose in May 2026", "quý 3 tăng tháng 5/2026"),  # month/year: no day part
        ("on October 40, 2026", "ngày 40/10/2026"),  # day out of range
        ("on October 5, 2026", "ngày 5/10/2027"),  # year not in the source
        ("on October 5, 2026", "ngày 5/11/2026"),  # month not named in the source
        ("on October 5, 2026", "ngày 5:10"),  # time, not a date
    ]
    for src, bad in not_dates:
        with pytest.raises(ValueError, match="new_token"):
            p.reject_new_tokens(src, bad)


def test_marker_collision_gets_fresh_nonce(monkeypatch):
    seq = iter(["aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(p.secrets, "token_hex", lambda n: next(seq))
    _, protected = p.protect_text("lit @@Paaaaaaaa_ 7")
    assert list(protected) == ["@@Pbbbbbbbb_0@@"]


def test_markdown_prefixes_and_fences_kept():
    text = "# Title 1\n- item 2\n\n```\ncode 3\n```\n> quote"
    lines = p.split_markdown(text)
    assert [b for _, b in lines if b] == ["Title 1", "item 2", "quote"]
    assert p.join_markdown(lines, ["T", "I", "Q"]) == "# T\n- I\n\n```\ncode 3\n```\n> Q"
