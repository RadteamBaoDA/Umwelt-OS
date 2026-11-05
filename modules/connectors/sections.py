"""Server-side RSS section catalog for the built-in news presets.

The owner picks section slugs; the URLs below are the only feed URLs a built-in can ever request, so no
owner input reaches a request line and every URL stays on the preset's declared host. Each provider's
``default`` section is exactly today's single feed URL, which keeps an unconfigured source byte-for-byte
unchanged. Every URL here was fetched once and returned valid RSS/Atom; add a section only after the same check.
No I/O and no imports from other connector modules (``provider_specs`` and ``providers.news`` import this).
"""

from dataclasses import dataclass

MAX_SECTIONS = 6  # per source; the pilot daily call cap is sized for this many GETs per sync


@dataclass(frozen=True)
class FeedSection:
    """One selectable feed: stable slug, English/Vietnamese labels and the absolute https feed URL."""

    slug: str
    label_en: str
    label_vi: str
    url: str


@dataclass(frozen=True)
class SectionCatalog:
    """A provider's selectable sections; ``default`` names the section equal to the preset's legacy feed."""

    default: str
    sections: tuple[FeedSection, ...]

    def by_slug(self) -> dict[str, FeedSection]:
        """Return the sections keyed by slug."""
        return {s.slug: s for s in self.sections}


def _catalog(
    default: str, base: str, rows: tuple[tuple[str, str, str, str], ...]
) -> SectionCatalog:
    """Build a catalog from ``(slug, label_en, label_vi, path)`` rows joined to ``base``."""
    return SectionCatalog(
        default, tuple(FeedSection(slug, en, vi, base + path) for slug, en, vi, path in rows)
    )


SECTION_CATALOG: dict[str, SectionCatalog] = {
    "tuoitre_news": _catalog(
        "home",
        "https://tuoitre.vn/",
        (
            ("home", "Home", "Trang chủ", "home.rss"),
            ("thoi-su", "Current affairs", "Thời sự", "thoi-su.rss"),
            ("the-gioi", "World", "Thế giới", "the-gioi.rss"),
            ("phap-luat", "Law", "Pháp luật", "phap-luat.rss"),
            ("kinh-doanh", "Business", "Kinh doanh", "kinh-doanh.rss"),
            ("cong-nghe", "Technology", "Công nghệ", "cong-nghe.rss"),
            ("nhip-song-so", "Digital life", "Nhịp sống số", "nhip-song-so.rss"),
            ("nhip-song-tre", "Youth life", "Nhịp sống trẻ", "nhip-song-tre.rss"),
            ("van-hoa", "Culture", "Văn hóa", "van-hoa.rss"),
            ("giai-tri", "Entertainment", "Giải trí", "giai-tri.rss"),
            ("the-thao", "Sports", "Thể thao", "the-thao.rss"),
            ("giao-duc", "Education", "Giáo dục", "giao-duc.rss"),
            ("suc-khoe", "Health", "Sức khỏe", "suc-khoe.rss"),
            ("xe", "Vehicles", "Xe", "xe.rss"),
            ("du-lich", "Travel", "Du lịch", "du-lich.rss"),
        ),
    ),
    "vnexpress_business": _catalog(
        "kinh-doanh",
        "https://vnexpress.net/rss/",
        (
            ("kinh-doanh", "Business", "Kinh doanh", "kinh-doanh.rss"),
            ("tin-moi-nhat", "Latest news", "Tin mới nhất", "tin-moi-nhat.rss"),
            ("thoi-su", "Current affairs", "Thời sự", "thoi-su.rss"),
            ("the-gioi", "World", "Thế giới", "the-gioi.rss"),
            ("phap-luat", "Law", "Pháp luật", "phap-luat.rss"),
            ("giao-duc", "Education", "Giáo dục", "giao-duc.rss"),
            ("suc-khoe", "Health", "Sức khỏe", "suc-khoe.rss"),
            ("giai-tri", "Entertainment", "Giải trí", "giai-tri.rss"),
            ("the-thao", "Sports", "Thể thao", "the-thao.rss"),
            ("du-lich", "Travel", "Du lịch", "du-lich.rss"),
            ("oto-xe-may", "Cars and bikes", "Ô tô - Xe máy", "oto-xe-may.rss"),
        ),
    ),
    "thanhnien_news": _catalog(
        "home",
        "https://thanhnien.vn/rss/",
        (
            ("home", "Home", "Trang chủ", "home.rss"),
            ("thoi-su", "Current affairs", "Thời sự", "thoi-su.rss"),
            ("the-gioi", "World", "Thế giới", "the-gioi.rss"),
            ("kinh-te", "Economy", "Kinh tế", "kinh-te.rss"),
            ("cong-nghe", "Technology", "Công nghệ", "cong-nghe.rss"),
            ("giao-duc", "Education", "Giáo dục", "giao-duc.rss"),
            ("the-thao", "Sports", "Thể thao", "the-thao.rss"),
            ("giai-tri", "Entertainment", "Giải trí", "giai-tri.rss"),
            ("suc-khoe", "Health", "Sức khỏe", "suc-khoe.rss"),
            ("doi-song", "Lifestyle", "Đời sống", "doi-song.rss"),
            ("du-lich", "Travel", "Du lịch", "du-lich.rss"),
        ),
    ),
    "dantri_news": _catalog(
        "home",
        "https://dantri.com.vn/rss/",
        (
            ("home", "Home", "Trang chủ", "home.rss"),
            ("su-kien", "Events", "Sự kiện", "su-kien.rss"),
            ("the-gioi", "World", "Thế giới", "the-gioi.rss"),
            ("kinh-doanh", "Business", "Kinh doanh", "kinh-doanh.rss"),
            ("xa-hoi", "Society", "Xã hội", "xa-hoi.rss"),
            ("the-thao", "Sports", "Thể thao", "the-thao.rss"),
            ("giai-tri", "Entertainment", "Giải trí", "giai-tri.rss"),
            ("giao-duc", "Education", "Giáo dục", "giao-duc.rss"),
            ("suc-khoe", "Health", "Sức khỏe", "suc-khoe.rss"),
            ("phap-luat", "Law", "Pháp luật", "phap-luat.rss"),
            ("suc-manh-so", "Digital power", "Sức mạnh số", "suc-manh-so.rss"),
            ("o-to-xe-may", "Cars and bikes", "Ô tô - Xe máy", "o-to-xe-may.rss"),
            ("du-lich", "Travel", "Du lịch", "du-lich.rss"),
        ),
    ),
    "bbc_world": _catalog(
        "world",
        "https://feeds.bbci.co.uk/news/",
        (
            ("world", "World", "Thế giới", "world/rss.xml"),
            ("top-stories", "Top stories", "Tin nổi bật", "rss.xml"),
            ("business", "Business", "Kinh doanh", "business/rss.xml"),
            ("technology", "Technology", "Công nghệ", "technology/rss.xml"),
            (
                "science",
                "Science and environment",
                "Khoa học và môi trường",
                "science_and_environment/rss.xml",
            ),
            ("health", "Health", "Sức khỏe", "health/rss.xml"),
            ("politics", "Politics", "Chính trị", "politics/rss.xml"),
            ("asia", "Asia", "Châu Á", "world/asia/rss.xml"),
            ("europe", "Europe", "Châu Âu", "world/europe/rss.xml"),
            ("us-canada", "US and Canada", "Mỹ và Canada", "world/us_and_canada/rss.xml"),
            (
                "entertainment",
                "Entertainment and arts",
                "Giải trí và nghệ thuật",
                "entertainment_and_arts/rss.xml",
            ),
        ),
    ),
    "npr_news": _catalog(
        "news",
        "https://feeds.npr.org/",
        (
            ("news", "News", "Tin tức", "1001/rss.xml"),
            ("national", "National", "Trong nước", "1003/rss.xml"),
            ("world", "World", "Thế giới", "1004/rss.xml"),
            ("politics", "Politics", "Chính trị", "1014/rss.xml"),
            ("business", "Business", "Kinh doanh", "1006/rss.xml"),
            ("technology", "Technology", "Công nghệ", "1019/rss.xml"),
            ("science", "Science", "Khoa học", "1007/rss.xml"),
            ("health", "Health", "Sức khỏe", "1128/rss.xml"),
            ("arts", "Arts and life", "Nghệ thuật và đời sống", "1008/rss.xml"),
        ),
    ),
    "fed_press": _catalog(
        "press_all",
        "https://www.federalreserve.gov/feeds/",
        (
            ("press_all", "All press releases", "Tất cả thông cáo báo chí", "press_all.xml"),
            ("press_monetary", "Monetary policy", "Chính sách tiền tệ", "press_monetary.xml"),
            ("press_bcreg", "Banking regulation", "Quản lý ngân hàng", "press_bcreg.xml"),
            (
                "press_orders",
                "Orders on banking applications",
                "Quyết định về hồ sơ ngân hàng",
                "press_orders.xml",
            ),
            (
                "press_enforcement",
                "Enforcement actions",
                "Biện pháp cưỡng chế",
                "press_enforcement.xml",
            ),
            ("press_other", "Other announcements", "Thông báo khác", "press_other.xml"),
            ("speeches", "Speeches", "Bài phát biểu", "speeches.xml"),
            ("testimony", "Testimony", "Lời khai trước Quốc hội", "testimony.xml"),
        ),
    ),
    "lobsters": _catalog(
        "front",
        "https://lobste.rs/",
        (
            ("front", "Front page", "Trang chủ", "rss"),
            ("newest", "Newest", "Mới nhất", "newest.rss"),
            ("ai", "AI", "AI", "t/ai.rss"),
            ("programming", "Programming", "Lập trình", "t/programming.rss"),
            ("security", "Security", "Bảo mật", "t/security.rss"),
            ("python", "Python", "Python", "t/python.rss"),
            ("rust", "Rust", "Rust", "t/rust.rss"),
            ("javascript", "JavaScript", "JavaScript", "t/javascript.rss"),
            ("web", "Web", "Web", "t/web.rss"),
            ("linux", "Linux", "Linux", "t/linux.rss"),
            ("databases", "Databases", "Cơ sở dữ liệu", "t/databases.rss"),
            ("devops", "DevOps", "DevOps", "t/devops.rss"),
        ),
    ),
    # Third-party mirror of github.com/trending. Fetched 2026-11-04; daily Python (1.6 MB) and daily TypeScript
    # (0.9 MB) are omitted because they approach or exceed the 1 MiB feed cap, so the weekly lists stand in.
    "github_trending": _catalog(
        "daily-all",
        "https://mshibanami.github.io/GitHubTrendingRSS/",
        (
            ("daily-all", "Daily: all languages", "Hàng ngày: mọi ngôn ngữ", "daily/all.xml"),
            ("weekly-all", "Weekly: all languages", "Hàng tuần: mọi ngôn ngữ", "weekly/all.xml"),
            (
                "weekly-typescript",
                "Weekly: TypeScript",
                "Hàng tuần: TypeScript",
                "weekly/typescript.xml",
            ),
            ("weekly-python", "Weekly: Python", "Hàng tuần: Python", "weekly/python.xml"),
            ("daily-go", "Daily: Go", "Hàng ngày: Go", "daily/go.xml"),
            ("daily-rust", "Daily: Rust", "Hàng ngày: Rust", "daily/rust.xml"),
            (
                "daily-javascript",
                "Daily: JavaScript",
                "Hàng ngày: JavaScript",
                "daily/javascript.xml",
            ),
        ),
    ),
    # We Work Remotely category feeds (all fetched 2026-11-04 with HTTP 200).
    "wwr_jobs": _catalog(
        "programming",
        "https://weworkremotely.com/categories/",
        (
            ("programming", "Programming", "Lập trình", "remote-programming-jobs.rss"),
            (
                "full-stack-programming",
                "Full-stack",
                "Full-stack",
                "remote-full-stack-programming-jobs.rss",
            ),
            (
                "back-end-programming",
                "Back-end",
                "Back-end",
                "remote-back-end-programming-jobs.rss",
            ),
            (
                "front-end-programming",
                "Front-end",
                "Front-end",
                "remote-front-end-programming-jobs.rss",
            ),
            (
                "devops-sysadmin",
                "DevOps and sysadmin",
                "DevOps và quản trị hệ thống",
                "remote-devops-sysadmin-jobs.rss",
            ),
        ),
    ),
}


def validate_sections(provider: str, slugs: tuple[str, ...] | list[str]) -> None:
    """Require 1..``MAX_SECTIONS`` unique slugs that all belong to ``provider``'s catalog.

    Raises ``ValueError`` for a provider without a catalog, a wrong count, a duplicate or an unknown slug.
    """
    catalog = SECTION_CATALOG.get(provider)
    if catalog is None:
        raise ValueError("Provider has no selectable sections")
    if not 1 <= len(slugs) <= MAX_SECTIONS or len(set(slugs)) != len(slugs):
        raise ValueError(f"Choose 1 to {MAX_SECTIONS} distinct sections")
    unknown = set(slugs) - catalog.by_slug().keys()
    if unknown:
        raise ValueError("Unknown section for this provider")
