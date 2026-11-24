"""
PARALAKS Haber Masası — AI sağlayıcı katmanı ve içerik üretimi.

AI_PROVIDER = "gemini"  -> Google Gemini Flash (ücretsiz katman)
AI_PROVIDER = "claude"  -> Anthropic Claude Haiku (düşük maliyet)

Her iki sağlayıcı da aynı arayüzü sunar: generate_json(system, user) -> dict | list
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

try:
    import trafilatura
except ImportError:  # trafilatura yoksa BeautifulSoup yedeği kullanılır
    trafilatura = None

from rss_fetcher import HTTP_HEADERS, NewsItem


class AIError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Sağlayıcılar
# --------------------------------------------------------------------------- #
def _parse_json(text: str):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Metnin içindeki ilk JSON bloğunu yakala
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        raise AIError(f"AI yanıtı JSON değil: {text[:200]}")
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise AIError(f"AI JSON çözümlenemedi: {exc}; yanıt: {text[:300]}") from exc


class _BaseAI:
    name = "base"

    def _post(self, url, headers, body, timeout=90, attempts=4):
        last = None
        for attempt in range(attempts):
            try:
                r = requests.post(url, headers=headers, json=body, timeout=timeout)
            except requests.RequestException as exc:
                last = exc
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code in (429, 500, 502, 503, 504, 529):
                last = AIError(f"{self.name} HTTP {r.status_code}: {r.text[:200]}")
                wait = 15 * (attempt + 1)
                m = re.search(r'"retryDelay":\s*"(\d+)s"', r.text)
                if m:
                    wait = max(wait, int(m.group(1)) + 1)
                if attempt < attempts - 1:
                    time.sleep(min(wait, 60))
                continue
            if r.status_code >= 400:
                raise AIError(f"{self.name} HTTP {r.status_code}: {r.text[:400]}")
            return r.json()
        raise AIError(f"{self.name} tekrar denemeler başarısız: {last}")

    def generate_json(self, system: str, user: str, max_tokens: int = 4096):
        raise NotImplementedError


class GeminiAI(_BaseAI):
    name = "gemini"

    def __init__(self, api_key: str, model: str, fallbacks: list[str] | None = None):
        if not api_key:
            raise AIError("GEMINI_API_KEY tanımlı değil")
        self.api_key, self.model = api_key, model
        self.models = [model] + [m for m in (fallbacks or []) if m and m != model]

    def generate_json(self, system, user, max_tokens=4096):
        """Model yoğunsa (503/429/zaman aşımı) veya kapalıysa (404) sıradaki yedek modele geçer."""
        errors = []
        for n, model in enumerate(self.models):
            try:
                return self._generate(model, system, user, max_tokens,
                                      attempts=2 if n < len(self.models) - 1 else 3)
            except AIError as exc:
                msg = str(exc)
                errors.append(f"{model}: {msg[:120]}")
                if any(code in msg for code in ("HTTP 404", "HTTP 429", "HTTP 503", "HTTP 500",
                                                "tekrar denemeler", "boş yanıt")):
                    print(f"[AI] {model} kullanılamadı, yedek modele geçiliyor", flush=True)
                    continue
                raise
        raise AIError("Tüm Gemini modelleri başarısız: " + " | ".join(errors))

    def _generate(self, model, system, user, max_tokens, attempts):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": 0.4,
                "responseMimeType": "application/json",
                # "Düşünen" modellerde düşünme token'ları da bu limite sayılır; geniş tutuyoruz
                "maxOutputTokens": max(max_tokens, 8192),
            },
        }
        data = self._post(url, {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}, body,
                          attempts=attempts)
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        except (KeyError, IndexError):
            raise AIError(f"Gemini boş yanıt döndü: {json.dumps(data)[:400]}")
        return _parse_json(text)


class ClaudeAI(_BaseAI):
    name = "claude"

    def __init__(self, api_key: str, model: str):
        if not api_key:
            raise AIError("ANTHROPIC_API_KEY tanımlı değil")
        self.api_key, self.model = api_key, model

    def generate_json(self, system, user, max_tokens=4096):
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": 0.4,
            "system": system + "\n\nYanıtı YALNIZCA geçerli JSON olarak ver; açıklama veya ``` ekleme.",
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        data = self._post("https://api.anthropic.com/v1/messages", headers, body)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return _parse_json(text)


def get_ai(cfg: dict) -> _BaseAI:
    provider = (cfg.get("AI_PROVIDER") or "gemini").strip().lower()
    if provider == "gemini":
        fallbacks = cfg.get("GEMINI_FALLBACK_MODELS") or [
            "gemini-flash-latest", "gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-3.1-flash-lite"]
        if isinstance(fallbacks, str):
            fallbacks = [m.strip() for m in fallbacks.split(",")]
        return GeminiAI(cfg.get("GEMINI_API_KEY", ""), cfg.get("GEMINI_MODEL") or "gemini-3.5-flash", fallbacks)
    if provider in ("claude", "anthropic"):
        return ClaudeAI(cfg.get("ANTHROPIC_API_KEY", ""), cfg.get("CLAUDE_MODEL") or "claude-haiku-5-5")
    raise AIError(f"Bilinmeyen AI_PROVIDER: {provider!r} (gemini | claude olmalı)")


# --------------------------------------------------------------------------- #
# 1) Ön eleme + Türkçe kısa özet (toplu, tek AI çağrısı)
# --------------------------------------------------------------------------- #
TRIAGE_SYSTEM = """Sen bir Türk video oyunu haber masasının ön eleme editörüsün.
Sana numaralı İngilizce haber başlıkları ve özetleri gelecek. Her biri için karar ver:

- "oyun_haberi": Haber doğrudan VİDEO OYUNLARI ile mi ilgili? (oyun duyurusu, fragman, çıkış tarihi,
  güncelleme, DLC, satış rakamı, stüdyo/yayıncı/sektör haberi, oyun mağazası indirimi, ücretsiz oyun,
  konsol/platform haberi -> true)
  Şunlar FALSE: oyun uyarlaması olsa bile dizi/film/anime haberleri, sinema, çizgi roman, kitap, oyuncak,
  masa oyunu, genel teknoloji, TV/kulaklık/SSD/monitör gibi donanım ve aksesuar indirim/affiliate reklamları,
  "en iyi X" alışveriş rehberleri.
- "oyun": Haber tek bir oyuna odaklanıyorsa oyunun resmi adı (orijinal yazımıyla), değilse "".
- "olay": Gelişmenin 4-8 kelimelik İngilizce, küçük harfli anahtarı (ör: "elden ring nightreign release date announced").
  Farklı sitelerin aynı haberini eşleştirmek için kullanılır; sade ve standart yaz.
- "baslik_tr": Türkçe, clickbait'siz, en fazla 90 karakterlik haber başlığı.
- "ozet_tr": Türkçe 1-2 cümlelik (en fazla 260 karakter) bilgi verici kısa özet. Uydurma bilgi ekleme.
- "tekrar": Bu haber, "DAHA ÖNCE GÖNDERİLENLER" listesindeki bir haberle ya da bu listede kendisinden
  ÖNCE gelen bir haberle AYNI GELİŞMEYİ mi anlatıyor? (farklı site, farklı kelimeler olabilir; ör. iki sitenin
  aynı mahkeme kararı, aynı fragman, aynı çıkış tarihi duyurusu haberi -> true). Aynı oyunun FARKLI bir
  gelişmesi (ör. biri yama, diğeri satış rakamı) -> false.

Çıktı: {"sonuclar": [{"i": 0, "oyun_haberi": true, "oyun": "", "olay": "", "baslik_tr": "", "ozet_tr": "", "tekrar": false}, ...]}
Her girdi için tam olarak bir sonuç döndür."""


def triage(ai: _BaseAI, items: list[NewsItem], recent_titles: list[str] | None = None) -> list[dict]:
    lines = []
    if recent_titles:
        lines.append("DAHA ÖNCE GÖNDERİLENLER (son saatler):\n" + "\n".join(f"- {t}" for t in recent_titles[-60:]))
        lines.append("DEĞERLENDİRİLECEK HABERLER:")
    for idx, it in enumerate(items):
        lines.append(
            f"[{idx}] Kaynak: {it.source}\nBaşlık: {it.title}\nEtiketler: {', '.join(it.tags[:8])}\n"
            f"Özet: {it.summary[:500]}"
        )
    data = ai.generate_json(TRIAGE_SYSTEM, "\n\n".join(lines), max_tokens=4096)
    results = data.get("sonuclar") if isinstance(data, dict) else data
    if not isinstance(results, list):
        raise AIError(f"Beklenmeyen triage çıktısı: {str(data)[:300]}")
    by_index = {}
    for r in results:
        try:
            by_index[int(r.get("i"))] = r
        except (TypeError, ValueError):
            continue
    if len(by_index) < len(items):
        raise AIError(f"AI {len(items)} haberin yalnızca {len(by_index)} tanesini döndürdü")
    return [by_index[i] for i in range(len(items))]


# --------------------------------------------------------------------------- #
# 2) Kaynak sayfadan tam metin + yüksek çözünürlüklü görsel
# --------------------------------------------------------------------------- #
STORE_DOMAINS = (
    "store.steampowered.com", "store.epicgames.com", "gog.com", "store.playstation.com",
    "nintendo.com", "xbox.com", "humblebundle.com", "ubisoft.com/store", "ea.com", "itch.io",
    "microsoft.com/store", "apps.apple.com", "play.google.com",
)

SOURCE_NAMES = {
    "gematsu.com": "Gematsu", "videogameschronicle.com": "VGC", "eurogamer.net": "Eurogamer",
    "polygon.com": "Polygon", "ign.com": "IGN", "steampowered.com": "Steam",
    "store.steampowered.com": "Steam", "gamespot.com": "GameSpot", "pcgamer.com": "PC Gamer",
    "kotaku.com": "Kotaku", "theverge.com": "The Verge", "rockpapershotgun.com": "Rock Paper Shotgun",
    "vg247.com": "VG247", "insider-gaming.com": "Insider Gaming", "gamesindustry.biz": "GamesIndustry.biz",
    "blog.playstation.com": "PlayStation Blog", "news.xbox.com": "Xbox Wire", "nintendo.com": "Nintendo",
    "automaton-media.com": "Automaton", "dualshockers.com": "DualShockers", "destructoid.com": "Destructoid",
}


def source_name_from_url(url: str, site_name: str | None = None) -> str:
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    for dom, name in SOURCE_NAMES.items():
        if host == dom or host.endswith("." + dom):
            return name
    return site_name or host


_WP_SIZE = re.compile(r"-\d{2,4}x\d{2,4}(?=\.(?:jpe?g|png|webp)$)", re.I)
_RESIZE_PARAMS = re.compile(r"(^|&)(w|h|width|height|quality|q|fit|crop|resize|format|auto|dpr)=", re.I)


def _url_ok(url: str) -> bool:
    try:
        r = requests.head(url, headers=HTTP_HEADERS, timeout=10, allow_redirects=True)
        if r.status_code == 405:  # HEAD desteklemeyen CDN'ler
            r = requests.get(url, headers=HTTP_HEADERS, timeout=10, stream=True)
            r.close()
        return r.status_code < 400 and r.headers.get("content-type", "image").startswith("image")
    except requests.RequestException:
        return False


def upgrade_image_url(url: str | None) -> str | None:
    """CDN küçültme parametrelerini ve WordPress boyut eklerini kaldırıp orijinal görseli dener."""
    if not url:
        return None
    candidate = url
    parts = urlsplit(url)
    if parts.query and _RESIZE_PARAMS.search(parts.query):
        candidate = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    p2 = urlsplit(candidate)
    if _WP_SIZE.search(p2.path):
        candidate = urlunsplit((p2.scheme, p2.netloc, _WP_SIZE.sub("", p2.path), p2.query, ""))
    if candidate != url and _url_ok(candidate):
        return candidate
    return url


def fetch_article(url: str) -> dict:
    r = requests.get(url, headers=HTTP_HEADERS, timeout=25)
    r.raise_for_status()
    page = r.text
    soup = BeautifulSoup(page, "html.parser")

    def meta(*names):
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return None

    title = meta("og:title", "twitter:title") or (soup.title.string.strip() if soup.title and soup.title.string else "")
    image = meta("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src")
    site_name = meta("og:site_name")

    text = ""
    if trafilatura is not None:
        try:
            text = trafilatura.extract(page, url=url, include_comments=False, include_tables=False,
                                       favor_precision=True) or ""
        except Exception:
            text = ""
    if len(text) < 300:
        body = soup.find("article") or soup.body or soup
        text = "\n\n".join(p.get_text(" ", strip=True) for p in body.find_all("p") if len(p.get_text(strip=True)) > 40)

    store_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("http") and any(d in href for d in STORE_DOMAINS) and href not in store_links:
            store_links.append(href)

    return {
        "url": r.url,
        "title": title,
        "text": text.strip(),
        "image": upgrade_image_url(image),
        "site_name": site_name,
        "store_links": store_links[:8],
    }


def fetch_article_safe(url: str, fallback: dict | None = None) -> dict:
    """Sayfayı çekmeyi dener; site engellerse (Cloudflare vb.) RSS'ten gelen metin/görsel ile devam eder.
    Dönen sözlükte 'limited' = True ise içerik yalnızca RSS özetine dayanıyordur."""
    fallback = fallback or {}
    error = None
    try:
        article = fetch_article(url)
    except Exception as exc:
        error = exc
        article = {"url": url, "title": "", "text": "", "image": None, "site_name": None, "store_links": []}
    fb_text = "\n\n".join(t for t in (fallback.get("content"), fallback.get("summary")) if t)
    article["limited"] = False
    if len(article["text"]) < 300 and fb_text:
        article["text"] = (article["text"] + "\n\n" + fb_text).strip()
        article["limited"] = True
    article["title"] = article["title"] or fallback.get("title", "")
    if not article["image"] and fallback.get("image"):
        article["image"] = upgrade_image_url(fallback["image"])
    if len(article["text"]) < 80:
        raise AIError(f"Kaynak sayfa okunamadı ve yedek metin yok ({error or 'metin bulunamadı'})")
    return article


# --------------------------------------------------------------------------- #
# 3) PARALAKS içerik paketi (Admin paneli + X metni)
# --------------------------------------------------------------------------- #
PACKAGE_SYSTEM = """Sen PARALAKS (paralaks.online) adlı bağımsız Türk video oyunu medyasının kıdemli haber editörüsün.

PARALAKS ÜSLUBU
- Analitik, sakin, bilgi odaklı ve akıcı Türkçe. Haberi bağlamıyla anlat: ne oldu, neden önemli, seriye/stüdyoya/sektöre etkisi.
- Clickbait YOK: "şok", "olay yarattı", "inanılmaz", "herkes konuşuyor", soru-başlık, gereksiz ünlem, büyük harf vurgusu kullanma.
- Kaynak metinde olmayan bilgi, tarih, fiyat ya da alıntı UYDURMA. Emin olmadığın şeyi yazma.
- Oyun, stüdyo ve platform adlarını orijinal yazımıyla bırak (The Witcher 4, PlayStation 5). Türkçe ekleri kesme işaretiyle ekle (Nintendo Switch 2'ye).
- Tarihleri Türkçe yaz (ör. 14 Mart 2026). Fiyatları kaynaktaki para birimiyle ver.
- Doğrudan alıntıları Türkçeye çevirip tırnak içinde ver ve kime ait olduğunu belirt.

ÜRETİLECEK JSON
{
  "baslik": "En fazla 100 karakter, bilgi veren Türkçe haber başlığı",
  "kisa_ozet": "1-2 cümle, en fazla 220 karakter; listeleme/meta açıklaması için",
  "ana_metin": "3-6 paragraf; paragrafları \\n\\n ile ayır. İlk paragraf haberin özü; sonrakiler detay ve bağlam. Başlık, madde işareti veya markdown kullanma.",
  "tek_oyun_odakli": true | false,
  "kunye": {
    "gelistirici": "", "yayinci": "",
    "cikis_tarihi": "Belli değilse tam olarak: Duyurulmadı",
    "platformlar": "PC, PS5, Xbox Series X|S ... (virgülle)",
    "tur": "Türkçe tür adı (ör. Aksiyon RPG)",
    "turkce": "Var" | "Yok"
  } | null,
  "haber_tipi": "standart" | "kampanya",
  "x_metni": "",
  "magaza_linki": "",
  "instagram_ozet": ["madde 1", "madde 2", "madde 3"]
}

KURALLAR
- Haber tek bir oyuna odaklanıyorsa "tek_oyun_odakli": true ve "kunye" doldurulur. Genel sektör haberi
  (işten çıkarma, satın alma, platform politikası, etkinlik, birden çok oyun listesi vb.) ise false ve "kunye": null.
- Künyede bilmediğin alanı tahminle doldurma: geliştirici/yayıncı bilinmiyorsa "Belirtilmedi" yaz.
  "turkce" yalnızca Türkçe dil desteği kaynakta ya da genel olarak kesin biliniyorsa "Var", aksi halde "Yok".
- "haber_tipi": İndirim, ücretsiz oyun, ücretsiz hafta sonu, abonelik kataloğu (PS Plus, Game Pass), mağaza kampanyası
  ise "kampanya"; duyuru, fragman, çıkış tarihi, güncelleme, sektör haberi ise "standart".
- "x_metni": X (Twitter) için Türkçe, tek cümlelik, sade paylaşım metni. Emoji, hashtag, kaynak adı ve link EKLEME
  (bunları sistem ekleyecek). En fazla 220 karakter.
- "magaza_linki": "kampanya" ise verilen mağaza linklerinden en uygun olanı; yoksa boş bırak. "standart" ise boş.
- "instagram_ozet": @paralaksonline Instagram gönderisi/karuseli için 3 veya 4 maddelik kısa özet.
  Her madde tek cümle, en fazla 90 karakter; haberin en önemli bilgilerini (ne, ne zaman, hangi platform,
  neden önemli) sırayla verir. Madde işareti, numara, emoji, hashtag ve link EKLEME (sistem biçimlendirecek).
  Aynı bilgiyi tekrarlama; PARALAKS üslubunda sade ve bilgi odaklı yaz."""


def generate_package(ai: _BaseAI, article: dict, source_name: str) -> dict:
    text = article.get("text") or ""
    note = ("\nNOT: Elimizde yalnızca kısa RSS özeti var. Metni kısa tut (1-2 paragraf) ve özette "
            "olmayan hiçbir ayrıntıyı ekleme.\n") if article.get("limited") else ""
    user = (
        note +
        f"Kaynak: {source_name}\nURL: {article['url']}\nOrijinal başlık: {article.get('title', '')}\n"
        f"Mağaza linkleri: {', '.join(article.get('store_links') or []) or 'yok'}\n\n"
        f"HABER METNİ:\n{text[:14000]}"
    )
    pkg = ai.generate_json(PACKAGE_SYSTEM, user, max_tokens=4096)
    if not isinstance(pkg, dict) or not pkg.get("baslik") or not pkg.get("ana_metin"):
        raise AIError(f"Eksik içerik paketi: {str(pkg)[:300]}")
    if not pkg.get("tek_oyun_odakli"):
        pkg["kunye"] = None
    if pkg.get("kunye"):
        k = pkg["kunye"]
        if not str(k.get("cikis_tarihi", "")).strip():
            k["cikis_tarihi"] = "Duyurulmadı"
        k["turkce"] = "Var" if str(k.get("turkce", "")).strip().lower() == "var" else "Yok"
    pkg["x_post"] = format_x_text(pkg, source_name, article["url"])
    pkg["instagram"] = clean_instagram(pkg.get("instagram_ozet"))
    return pkg


IG_MAX_ITEM = 110


def clean_instagram(items) -> list[str]:
    """AI'nin Instagram maddelerini temizler: 3-4 madde, baştaki işaretler/numaralar ve hashtag'ler atılır."""
    if isinstance(items, str):
        items = [s for s in re.split(r"\n+|(?<=[.!?])\s+(?=[A-ZÇĞİÖŞÜ])", items) if s.strip()]
    out = []
    for it in items or []:
        s = re.sub(r"^\s*(?:[-•*▪►▶✓✔·]|\d+[.)])\s*", "", str(it))
        s = re.sub(r"\s*#\w+", "", s).strip()
        if s:
            out.append(_cut(s, IG_MAX_ITEM))
    return out[:4]


X_LIMIT = 260
X_URL_WEIGHT = 23  # X her linki 23 karakter sayar


def _cut(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:-–")
    return cut + "…"


def format_x_text(pkg: dict, source_name: str, url: str) -> str:
    base = pkg.get("x_metni") or pkg.get("kisa_ozet") or pkg.get("baslik", "")
    if pkg.get("haber_tipi") == "kampanya":
        link = pkg.get("magaza_linki") or url
        body = _cut(base, X_LIMIT - X_URL_WEIGHT - 1)
        return f"{body} {link}"
    # Standart: başta emoji için bir boşluk bırakılır -> " Metin (Kaynak)"
    suffix = f" ({source_name})"
    body = _cut(base, X_LIMIT - len(suffix) - 1)
    return f" {body}{suffix}"


def x_length(text: str) -> int:
    return len(re.sub(r"https?://\S+", "x" * X_URL_WEIGHT, text))
