"""
PARALAKS Haber Masası — RSS tarama, kural tabanlı ön filtre ve kopya (duplicate) kontrolü.

Akış:
  fetch_all()      -> tüm akışları çeker, NewsItem listesi döner
  rule_filter()    -> dizi/film/çizgi roman/donanım reklamı gibi içerikleri AI'ye gitmeden eler
  find_duplicate() -> son X saatte aynı oyun / aynı gelişme gönderildiyse eşleşen kaydı döner
"""
from __future__ import annotations

import calendar
import hashlib
import html
import re
import time
from dataclasses import dataclass, field, asdict
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import feedparser
import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 ParalaksNewsBot/1.0"
)
HTTP_HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}


# --------------------------------------------------------------------------- #
# Veri modeli
# --------------------------------------------------------------------------- #
@dataclass
class NewsItem:
    id: str
    source: str
    title: str
    url: str
    summary: str = ""
    image: str | None = None
    published: float = 0.0
    tags: list[str] = field(default_factory=list)
    trusted: bool = False  # Gematsu/Steam gibi neredeyse tamamen oyun odaklı kaynaklar
    content: str = ""      # RSS içindeki tam metin (varsa); kaynak sayfa engellenirse yedek olarak kullanılır

    def to_dict(self) -> dict:
        return asdict(self)


TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_|cmpid|icid|taid|abt)", re.I)


def normalize_url(url: str) -> str:
    """Takip parametrelerini, #fragment'ı ve sondaki / işaretini temizler."""
    try:
        parts = urlsplit(url.strip())
        query = [(k, v) for k, v in parse_qsl(parts.query) if not TRACKING_PARAMS.match(k)]
        path = parts.path.rstrip("/") or "/"
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))
    except Exception:
        return url.strip()


def item_id(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:12]


def clean_html(text: str) -> str:
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    return re.sub(r"\s+", " ", html.unescape(soup.get_text(" "))).strip()


# --------------------------------------------------------------------------- #
# RSS çekme
# --------------------------------------------------------------------------- #
def _entry_image(entry) -> str | None:
    for key in ("media_content", "media_thumbnail"):
        media = entry.get(key) or []
        best, best_w = None, -1
        for m in media:
            url = m.get("url")
            if not url:
                continue
            try:
                w = int(m.get("width") or 0)
            except ValueError:
                w = 0
            if w > best_w:
                best, best_w = url, w
        if best:
            return best
    for enc in entry.get("enclosures") or []:
        if str(enc.get("type", "")).startswith("image") and enc.get("href"):
            return enc["href"]
    # Özet/HTML içindeki ilk <img>
    for blob in [entry.get("summary", "")] + [c.get("value", "") for c in entry.get("content") or []]:
        m = re.search(r'<img[^>]+src="([^"]+)"', blob or "")
        if m:
            return html.unescape(m.group(1))
    return None


def _entry_time(entry) -> float:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return float(calendar.timegm(t))
    return time.time()


def fetch_feed(feed: dict, timeout: int = 20) -> list[NewsItem]:
    resp = requests.get(feed["url"], headers=HTTP_HEADERS, timeout=timeout)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    items: list[NewsItem] = []
    for e in parsed.entries:
        link = e.get("link")
        title = clean_html(e.get("title", ""))
        if not link or not title:
            continue
        items.append(
            NewsItem(
                id=item_id(link),
                source=feed["name"],
                title=title,
                url=link,
                summary=clean_html(e.get("summary", ""))[:800],
                image=_entry_image(e),
                published=_entry_time(e),
                tags=[t.get("term", "") for t in e.get("tags") or [] if t.get("term")],
                trusted=bool(feed.get("trusted", False)),
                content=clean_html(" ".join(c.get("value", "") for c in e.get("content") or []))[:8000],
            )
        )
    return items


def rss_lookup(url: str, feeds: list[dict]) -> NewsItem | None:
    """Engellenen bir link için, aynı alan adındaki RSS akışında o haberi arar."""
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    target = item_id(url)
    for feed in feeds:
        if host and host.split(".")[-2] in urlsplit(feed["url"]).netloc.lower():
            try:
                for it in fetch_feed(feed):
                    if it.id == target:
                        return it
            except Exception:
                pass
    return None


def fetch_all(feeds: list[dict], max_age_hours: float, log=print) -> list[NewsItem]:
    cutoff = time.time() - max_age_hours * 3600
    out: list[NewsItem] = []
    for feed in feeds:
        if not feed.get("enabled", True):
            continue
        try:
            items = fetch_feed(feed)
            fresh = [i for i in items if i.published >= cutoff]
            log(f"[RSS] {feed['name']}: {len(items)} haber, {len(fresh)} tanesi son {max_age_hours:g} saatte")
            out.extend(fresh)
        except Exception as exc:  # bir kaynak çökerse diğerleri devam etsin
            log(f"[RSS] {feed['name']} okunamadı: {exc}")
    return out


# --------------------------------------------------------------------------- #
# Kural tabanlı ön filtre (AI'ye gitmeden ucuz eleme)
# --------------------------------------------------------------------------- #
GAME_TERMS = [
    "video game", "videogame", "game", "games", "gaming", "gameplay", "playstation", "ps5", "ps4",
    "ps vr2", "xbox", "nintendo", "switch 2", "steam", "steam deck", "epic games", "pc", "rpg",
    "jrpg", "mmo", "dlc", "expansion", "patch", "early access", "demo", "remaster", "remake",
    "developer", "studio", "publisher", "console", "multiplayer", "co-op", "shooter", "roguelike",
    "soulslike", "indie", "esports", "game pass", "playtest", "beta", "release date", "launches",
    "season pass", "battle pass", "open world", "metacritic", "speedrun", "mod", "mods",
]

NON_GAME_TERMS = [
    "movie", "movies", "film", "films", "box office", "tv series", "tv show", "television series",
    "series premiere", "series finale", "episode", "episodes", "showrunner", "netflix", "hbo",
    "hbo max", "disney+", "disney plus", "prime video", "apple tv", "paramount+", "peacock",
    "crunchyroll", "anime", "comic", "comics", "graphic novel", "manga", "screenplay",
    "cast", "casting", "actor", "actress", "director's cut", "documentary", "blu-ray", "4k uhd",
    "mcu", "marvel studios", "dc studios", "lego", "funko", "toy", "toys", "board game",
    "tabletop", "dungeons & dragons", "d&d", "magic: the gathering", "trading card", "novel",
    "book", "books", "album", "soundtrack vinyl", "theater", "theatrical", "rotten tomatoes",
]
NON_GAME_REGEX = [re.compile(r"\bseason \d+\b"), re.compile(r"\bs\d+e\d+\b")]

DEAL_TERMS = [
    "deal", "deals", "discount", "% off", "sale", "save $", "lowest price", "price drop",
    "black friday", "cyber monday", "prime day", "coupon", "bundle deal", "clearance",
]
RETAILER_TERMS = ["amazon", "best buy", "walmart", "target", "newegg", "b&h", "costco", "gamestop"]
HARDWARE_TERMS = [
    "tv", "oled", "qled", "monitor", "headphones", "headset", "earbuds", "airpods", "laptop",
    "ssd", "nvme", "microsd", "sd card", "hard drive", "gaming chair", "keyboard", "mouse",
    "router", "gpu", "graphics card", "rtx", "radeon", "cpu", "processor", "ipad", "iphone",
    "macbook", "tablet", "smartwatch", "vacuum", "projector", "soundbar", "speaker", "controller",
    "charger", "power bank", "webcam", "streaming deck", "capture card", "vpn",
]
# Oyun mağazası / ücretsiz oyun kampanyaları -> indirim olsa bile GEÇER
STORE_CAMPAIGN_TERMS = [
    "steam sale", "steam summer sale", "steam winter sale", "steam next fest", "free on steam",
    "playstation store", "ps store", "playstation plus", "ps plus", "eshop", "epic games store",
    "free game", "free games", "free to keep", "free-to-keep", "gog", "humble bundle",
    "prime gaming", "game pass", "xbox store", "free weekend", "free-to-play", "free to play",
    "fanatical", "green man gaming", "humble store", "steam key", "steam keys",
]
# Haber olmayan rehber / walkthrough / alışveriş listesi başlıkları
GUIDE_REGEX = [
    re.compile(r"^how to\b"), re.compile(r"\bwalkthrough\b"), re.compile(r"\blocation guide\b"),
    re.compile(r"\bwhere to find\b"), re.compile(r"\bevery [\w\s’']+ location"), re.compile(r"\bguide:"),
    re.compile(r"\b(?:best|top \d+) [\w\s-]+ (?:deals|to buy|gifts)\b"), re.compile(r"\btier list\b"),
    re.compile(r"\bcheats?\b.*\bcodes?\b"), re.compile(r"\bwordle\b"), re.compile(r"\bhub$"),
    re.compile(r"\bbest (?:builds?|loadouts?|settings)\b"),
]


def _count_terms(text: str, terms: list[str]) -> int:
    n = 0
    for t in terms:
        if re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", text):
            n += 1
    return n


def rule_filter(item: NewsItem, extra_block: list[str] | None = None) -> tuple[bool, str]:
    """(geçti_mi, neden) döner. Belirsiz durumlar GEÇER; son kararı AI bağlam filtresi verir."""
    text = " ".join([item.title, item.summary, " ".join(item.tags)]).lower()
    title = item.title.lower()

    for word in extra_block or []:
        if word.lower() in text:
            return False, f"kara liste: '{word}'"

    game = _count_terms(text, GAME_TERMS)
    tag_text = " ".join(item.tags).lower()
    if "game" in tag_text or "gaming" in tag_text:
        game += 2
    non_game = _count_terms(text, NON_GAME_TERMS) + sum(1 for r in NON_GAME_REGEX if r.search(text))
    store = _count_terms(text, STORE_CAMPAIGN_TERMS)
    deal = _count_terms(title, DEAL_TERMS) or _count_terms(text, ["black friday", "cyber monday", "prime day"])
    hardware = _count_terms(text, HARDWARE_TERMS)
    retailer = _count_terms(text, RETAILER_TERMS)

    if any(r.search(title) for r in GUIDE_REGEX):
        return False, "rehber/walkthrough içeriği (haber değil)"

    # 1) İndirim içerikleri: mağaza kampanyası ise geç, donanım/perakende reklamıysa ele
    if deal:
        if store:
            return True, "oyun mağazası kampanyası"
        if hardware:
            return False, "donanım/aksesuar indirim reklamı"
        if retailer:
            return False, "perakende (Amazon vb.) indirim içeriği"

    if item.trusted:
        return True, "güvenilir oyun kaynağı"

    # 2) Dizi/film/çizgi roman: oyun sinyali yoksa veya baskınsa ele
    if non_game and (game == 0 or non_game > game):
        return False, f"oyun dışı medya (film/dizi/çizgi roman) [oyun={game}, oyun-dışı={non_game}]"

    # Donanım kelimesi geçen ama indirim olmayan içerikler (ör. "GTA 6'daki laptop şifresi")
    # kural ile elenmez; kararı AI bağlam filtresi verir.
    return True, f"geçti [oyun={game}, oyun-dışı={non_game}]"


# --------------------------------------------------------------------------- #
# Kopya (duplicate) kontrolü
# --------------------------------------------------------------------------- #
_STOP = {"the", "a", "an", "of", "and", "for", "to", "in", "on", "is", "with", "new", "ve", "ile", "bir"}


def normalize_game(name: str | None) -> str:
    if not name:
        return ""
    s = name.lower()
    s = re.sub(r"[™®©:'’\"!?.,\-–—()]", " ", s)
    words = [w for w in s.split() if w not in _STOP]
    return " ".join(words)


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9çğıöşü]+", (text or "").lower()) if w not in _STOP and len(w) > 1}


def similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def find_duplicate(game: str, event: str, title: str, sent: list[dict], hours: float,
                   same_game_blocks: bool = True, threshold: float = 0.5) -> dict | None:
    """Son `hours` saatte gönderilmiş, aynı oyun ya da aynı gelişmeye ait kaydı döner."""
    cutoff = time.time() - hours * 3600
    g = normalize_game(game)
    for rec in reversed(sent):
        if rec.get("ts", 0) < cutoff:
            continue
        if same_game_blocks and g and g == normalize_game(rec.get("game")):
            return rec
        if event and rec.get("event"):
            shared = _tokens(event) & _tokens(rec["event"])
            if len(shared) >= 3 and similarity(event, rec["event"]) >= threshold:
                return rec
        if similarity(title, rec.get("title", "")) >= 0.6:
            return rec
    return None
