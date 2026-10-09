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
    literals = ("+12.5%", "1,250 USD", "2026-10-08", "[3]", "https://a.io/x?y=1", "`code 42`", "(AAPL)", "$TSLA",
                "https://b.io/p")
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
