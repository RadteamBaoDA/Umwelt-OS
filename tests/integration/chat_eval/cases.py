"""Graded chat eval cases shared by the real-model eval run and its unit tests.

``CASES`` is the frozen 45-case set; ``NEW_CASES`` is reserved for later additions and
``ALL_CASES`` is the concatenation used by the runner.
"""

from __future__ import annotations

from dataclasses import dataclass

DISTRACTORS = (
    55555.5,
    44444.4,
    33333.3,
)  # paused/archived/local_only binance rows: never an answer


@dataclass(frozen=True)
class Case:
    """One graded question: expected facts come from the matrix seed."""

    id: str
    cat: str
    lang: str  # "en" | "vi"
    q: str
    nums: tuple[float, ...] = ()  # every one must appear in the answer
    assoc: tuple[tuple[str, float], ...] = ()  # (label regex, number) must sit within 90 chars
    texts: tuple[str, ...] = ()  # every regex must match (case-insensitive)
    forbid_nums: tuple[float, ...] = ()
    forbid_texts: tuple[str, ...] = ()
    empty: bool = False  # must say there is no data and state no figure of its own
    nums_or_nodata: bool = False  # "today": either the latest value or a no-data statement
    history: tuple[tuple[str, str], ...] = ()  # prior (user, assistant) turns
    gold_keys: tuple[str, ...] = ()  # source keys whose rows must be cited
    forbid_keys: tuple[str, ...] = ()  # source keys that must never be cited
    source_scope: tuple[str, ...] = ()  # source keys the answer is scoped to
    min_recall: float | None = None  # minimum share of gold_facts the answer must state
    gold_facts: tuple[str, ...] = ()  # facts a complete answer must contain
    needs_sql: bool = False  # true when answering requires the SQL query path


HANOI = r"h[aà] n[oộ]i|hanoi"
HCMC = r"h[oồ] ch[ií] minh|hcmc|tp\.? ?hcm|s[aà]i g[oò]n"
CASES: tuple[Case, ...] = (
    Case(
        "btc_latest_en",
        "latest_price",
        "en",
        "What is the latest BTCUSDT price?",
        (67500,),
        forbid_nums=DISTRACTORS,
    ),
    Case(
        "btc_latest_vi",
        "latest_price",
        "vi",
        "Giá BTCUSDT mới nhất là bao nhiêu?",
        (67500,),
        forbid_nums=DISTRACTORS,
    ),
    Case(
        "btc_15nov_en",
        "latest_price",
        "en",
        "What was the BTCUSDT price on 15 November?",
        (67123.45,),
        # binance keeps one current revision per series: 15 Nov is history (current_only=false) or no data
        nums_or_nodata=True,
    ),
    Case(
        "btc_yesterday_vi",
        "relative_date",
        "vi",
        "Hôm qua BTCUSDT giá bao nhiêu?",
        (67500,),
        forbid_nums=(67123.45, *DISTRACTORS),
    ),
    Case(
        "btc_yesterday_en",
        "relative_date",
        "en",
        "What was the BTCUSDT price yesterday?",
        (67500,),
        forbid_nums=(67123.45, *DISTRACTORS),
    ),
    Case(
        "btc_today_vi",
        "relative_date",
        "vi",
        "Hôm nay BTCUSDT giá bao nhiêu?",
        (67500,),
        nums_or_nodata=True,
        forbid_nums=DISTRACTORS,
    ),
    Case(
        "btc_today_en",
        "relative_date",
        "en",
        "What is the BTCUSDT price today?",
        (67500,),
        nums_or_nodata=True,
        forbid_nums=DISTRACTORS,
    ),
    Case(
        "btc_avg_en",
        "aggregate",
        "en",
        "What is the average BTCUSDT price since 1 November?",
        (67500,),  # one current revision per binance series
        forbid_nums=DISTRACTORS,
    ),
    Case(
        "btc_avg_vi",
        "aggregate",
        "vi",
        "Giá trung bình của BTCUSDT từ đầu tháng 11 đến nay là bao nhiêu?",
        (67500,),  # one current revision per binance series
        forbid_nums=DISTRACTORS,
    ),
    Case(
        "btc_minmax_en",
        "aggregate",
        "en",
        "What are the highest and lowest BTCUSDT prices since 1 November?",
        (67500,),
    ),
    Case(
        "btc_minmax_vi",
        "aggregate",
        "vi",
        "Giá BTCUSDT cao nhất và thấp nhất kể từ ngày 1/11?",
        (67500,),
    ),
    Case("fx_en", "fx", "en", "What is the USD to VND exchange rate?", (26312.5,)),
    Case("fx_vi", "fx", "vi", "Tỷ giá USD/VND hiện tại là bao nhiêu?", (26312.5,)),
    Case(
        "gdp2024_en",
        "macro",
        "en",
        "What was Vietnam's GDP in 2024?",
        (500000000000.5,),
        forbid_nums=(476388000000.5,),
    ),
    Case(
        "gdp2024_vi",
        "macro",
        "vi",
        "GDP của Việt Nam năm 2024 là bao nhiêu?",
        (500000000000.5,),
        forbid_nums=(476388000000.5,),
    ),
    Case("gdp2023_en", "macro", "en", "What was Vietnam's GDP in 2023?", (433000000000,)),
    Case("gdp2025_vi", "empty", "vi", "GDP của Việt Nam năm 2025 là bao nhiêu?", empty=True),
    Case(
        "weather_avg_en",
        "weather",
        "en",
        "Compare the average temperature in Hanoi and Ho Chi Minh City.",
        (21, 31),
        assoc=((HANOI, 21), (HCMC, 31)),
    ),
    Case(
        "weather_avg_vi",
        "weather",
        "vi",
        "So sánh nhiệt độ trung bình giữa Hà Nội và TP.HCM.",
        (21, 31),
        assoc=((HANOI, 21), (HCMC, 31)),
    ),
    Case(
        "weather_latest_hanoi_en",
        "weather",
        "en",
        "What is the latest temperature in Hanoi?",
        (22,),
    ),
    Case("weather_max_hcmc_vi", "weather", "vi", "Nhiệt độ cao nhất ở TP.HCM là bao nhiêu?", (32,)),
    Case(
        "quake_strongest_en",
        "quake",
        "en",
        "In the USGS data, what is the strongest earthquake and how deep was it?",
        (6.3, 60.5),
    ),
    Case(
        "quake_strongest_vi",
        "quake",
        "vi",
        "Trận động đất mạnh nhất trong dữ liệu USGS có độ lớn và độ sâu bao nhiêu?",
        (6.3, 60.5),
    ),
    Case(
        "quake_10nov_en",
        "quake",
        "en",
        "Which USGS earthquake happened on 10 November (my local day)? Give its magnitude.",
        (5.1,),
        forbid_nums=(4.2, 6.3),
    ),
    Case(
        "quake_11nov_vi",
        "quake",
        "vi",
        "Ngày 11/11 trong dữ liệu USGS có trận động đất nào không? Độ lớn bao nhiêu?",
        (4.2,),
        forbid_nums=(5.1, 6.3),
    ),
    Case(
        "cve_vendor_en",
        "cve",
        "en",
        "Which vendor is affected by CVE-2016-3081 and is it used in ransomware campaigns?",
        texts=(r"apache", r"known|yes|used"),
    ),
    Case(
        "cve_vendor_vi",
        "cve",
        "vi",
        "CVE-2016-3081 thuộc nhà cung cấp nào và có bị dùng trong tấn công ransomware không?",
        texts=(r"apache", r"known|có|đã biết"),
    ),
    Case(
        "cve_count_en",
        "cve",
        "en",
        "How many vulnerabilities are in the CISA KEV data I collected?",
        (2,),
    ),
    Case("cve_product_vi", "cve", "vi", "CVE-2015-5477 ảnh hưởng sản phẩm nào?", texts=(r"bind",)),
    Case(
        "news_accentless_vi",
        "news",
        "vi",
        "Co tin gi ve gia vang khong?",
        texts=(r"giá vàng tăng mạnh|gia vang tang manh",),
    ),
    Case(
        "news_gold_en",
        "news",
        "en",
        "Is there any news about gold prices?",
        texts=(r"giá vàng tăng mạnh|gia vang tang manh|gold",),
    ),
    Case(
        "news_rate_vi",
        "news",
        "vi",
        "Tin nào nói ngân hàng hạ lãi suất?",
        texts=(r"lai suat giam|lãi suất giảm",),
    ),
    Case(
        "jobs_en",
        "jobs",
        "en",
        "What is the maximum salary of the Python Developer job and in which city?",
        (3000,),
        texts=(r"h[oồ] ch[ií] minh|ho chi minh",),
    ),
    Case(
        "jobs_vi", "jobs", "vi", "Công việc Python Developer trả lương tối đa bao nhiêu?", (3000,)
    ),
    Case(
        "arxiv_en",
        "research",
        "en",
        "Who are the authors of the arXiv paper about sparse attention?",
        texts=(r"ada lovelace", r"alan turing"),
    ),
    Case(
        "arxiv_vi",
        "research",
        "vi",
        "Bài báo arXiv về sparse attention do ai viết?",
        texts=(r"ada lovelace", r"alan turing"),
    ),
    Case(
        "bsky_en",
        "social",
        "en",
        "What did alice.example.com post on Bluesky?",
        texts=(r"shipping today",),
    ),
    Case(
        "bsky_vi",
        "social",
        "vi",
        "Gần đây trên Bluesky có bài đăng nào?",
        texts=(r"shipping today",),
    ),
    Case(
        "followup_btc_vi",
        "followup",
        "vi",
        "Cập nhật lại giá đi, giờ là bao nhiêu?",
        (67500,),
        forbid_nums=(66000,),
        history=(("Giá BTCUSDT mới nhất?", "BTCUSDT hiện là 66.000 USDT."),),
    ),
    Case(
        "followup_fx_en",
        "followup",
        "en",
        "Check again, what is it now?",
        (26312.5,),
        forbid_nums=(25000,),
        history=(("What is the USD/VND rate?", "It is 25,000 VND per USD."),),
    ),
    Case("empty_eth_en", "empty", "en", "What is the latest ETHUSDT price?", empty=True),
    Case("empty_danang_vi", "empty", "vi", "Nhiệt độ ở Đà Nẵng hôm nay là bao nhiêu?", empty=True),
    Case("empty_stock_en", "empty", "en", "What was the VNM stock price today?", empty=True),
    Case(
        "local_calendar_en",
        "local_only",
        "en",
        "What is on my calendar on 18 November?",
        forbid_texts=(r"standup", r"10:30"),
        empty=True,
    ),
    Case(
        "local_calendar_vi",
        "local_only",
        "vi",
        "Lịch của tôi ngày 18/11 có cuộc họp nào?",
        forbid_texts=(r"standup", r"10:30"),
        empty=True,
    ),
)

NEW_CASES: tuple[Case, ...] = ()
ALL_CASES: tuple[Case, ...] = CASES + NEW_CASES
