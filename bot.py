"""
PARALAKS Haber Masası — ana orkestratör.

Her çalıştırmada (GitHub Actions cron veya yerelde elle):
  1. Telegram'daki bekleyen güncellemeleri işler  -> [Haberi Hazırla] / [Pas Geç] butonları, gruba yapıştırılan linkler
  2. RSS akışlarını tarar, filtreler, kopyaları susturur, yeni haberleri gruba kart olarak atar
  3. (opsiyonel) LISTEN_SECONDS kadar Telegram'ı dinlemeye devam eder -> butonlara hızlı tepki
  4. state.json'u kaydeder (workflow bunu repoya geri commit'ler)

Kullanım:
  python bot.py              tam döngü
  python bot.py --test       bağlantı testi (Telegram + AI) ve gruba örnek kart
  python bot.py --url URL    verilen link için doğrudan içerik paketi üret ve gruba gönder
  python bot.py --dry-run    sadece RSS + kural filtresi; Telegram/AI çağrısı yok, state yazılmaz
  python bot.py --listen 300 RSS sonrası 300 sn boyunca butonları dinle
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

import content_generator as cg
import rss_fetcher as rf

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_CONFIG_PATH = ROOT / "config.example.json"

SECRET_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GEMINI_API_KEY", "ANTHROPIC_API_KEY")
ENV_OVERRIDES = SECRET_KEYS + ("AI_PROVIDER", "GEMINI_MODEL", "CLAUDE_MODEL", "LISTEN_SECONDS")


for _stream in (sys.stdout, sys.stderr):  # Windows konsolunda emoji/Türkçe karakter hatası olmasın
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# --------------------------------------------------------------------------- #
# Ayarlar ve durum
# --------------------------------------------------------------------------- #
def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config() -> dict:
    _load_dotenv(ROOT / ".env")
    cfg = json.loads(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))  # varsayılanlar
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    for key in ENV_OVERRIDES:  # ortam değişkenleri (GitHub Secrets) her zaman önceliklidir
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    # örnek dosyadaki yer tutucuları boş say
    for key in SECRET_KEYS:
        if str(cfg.get(key, "")).startswith("BURAYA"):
            cfg[key] = ""
    return cfg


def load_state() -> dict:
    state = {"telegram_offset": 0, "seen": {}, "sent": [], "pending": {}}
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8") or "{}"))
        except json.JSONDecodeError:
            log("[STATE] state.json bozuk, sıfırdan başlanıyor")
    return state


STATE_LIMITS = {"seen": 5000, "sent": 1000, "pending": 300}  # gün sınırına ek güvenlik tavanı


def prune_state(state: dict, cfg: dict, now: float | None = None) -> dict:
    """Bakım kuralı: `state_retention_days` (varsayılan 14) günden eski tüm kayıtları siler.
    Kayıt sayısı yine de tavanı aşarsa en eskiler atılır; böylece state.json hiçbir zaman şişmez."""
    now = now or time.time()
    cutoff = now - float(cfg.get("state_retention_days", 14)) * 86400
    seen = {k: v for k, v in state.get("seen", {}).items() if v > cutoff}
    state["seen"] = dict(sorted(seen.items(), key=lambda kv: kv[1])[-STATE_LIMITS["seen"]:])
    state["sent"] = [r for r in state.get("sent", []) if r.get("ts", 0) > cutoff][-STATE_LIMITS["sent"]:]
    pending = {k: v for k, v in state.get("pending", {}).items() if v.get("ts", 0) > cutoff}
    state["pending"] = dict(sorted(pending.items(), key=lambda kv: kv[1].get("ts", 0))[-STATE_LIMITS["pending"]:])
    return state


def save_state(state: dict, cfg: dict) -> None:
    prune_state(state, cfg)
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
class TelegramError(RuntimeError):
    pass


class TelegramRateLimited(TelegramError):
    """Telegram uzun süreli hız sınırı uyguluyor; bu çalıştırmada Telegram'a bir daha istek atılmaz."""


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.chat_id = str(chat_id)
        self.limited = False  # True olunca bu çalıştırmada hiçbir Telegram isteği gönderilmez

    def call(self, method: str, data: dict | None = None, files=None, timeout: int = 40):
        if self.limited:
            raise TelegramRateLimited(f"{method}: Telegram hız sınırı aktif, istek gönderilmedi")
        payload = {}
        for k, v in (data or {}).items():
            if v is None:
                continue
            payload[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
        for attempt in range(2):
            try:
                r = requests.post(self.base + method, data=payload, files=files, timeout=timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                # Geçici ağ hatası: dosya yüklemeyen çağrıları bir kez daha dene
                if attempt == 1 or files:
                    raise TelegramError(f"{method}: ağ hatası ({exc.__class__.__name__})") from exc
                log(f"[TG] {method} ağ hatası, 10 sn sonra bir kez daha denenecek")
                time.sleep(10)
                continue
            j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if j.get("ok"):
                return j["result"]
            retry = int((j.get("parameters") or {}).get("retry_after") or 0)
            if r.status_code == 429:
                # Kısa bekleme (grup mesaj limiti) -> bekle ve bir kez tekrar dene.
                # Uzun bekleme -> Telegram'ı bu çalıştırmada tamamen bırak (sınırın uzamasını önler).
                if retry <= 5 and attempt == 0:
                    time.sleep(retry + 1)
                    continue
                self.limited = True
                raise TelegramRateLimited(f"{method}: Too Many Requests (retry after {retry})")
            raise TelegramError(f"{method}: {j.get('description') or r.text[:200]}")

    @staticmethod
    def keyboard(rows):
        return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}

    @staticmethod
    def reply_to(message_id):
        return {"message_id": message_id, "allow_sending_without_reply": True} if message_id else None

    def send_text(self, text, reply_to=None, markup=None, preview=False):
        return self.call("sendMessage", {
            "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": not preview},
            "reply_parameters": self.reply_to(reply_to), "reply_markup": markup,
        })

    def send_photo(self, photo_url, caption, markup=None, reply_to=None):
        base = {"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML",
                "reply_markup": markup, "reply_parameters": self.reply_to(reply_to)}
        try:
            return self.call("sendPhoto", {**base, "photo": photo_url})
        except TelegramError:
            # Telegram bazı CDN'lerden çekemiyor -> indirip yükle
            img = download(photo_url, max_mb=10)
            return self.call("sendPhoto", base, files={"photo": ("kapak.jpg", img)})

    def send_document(self, content: bytes, filename: str, caption: str, reply_to=None):
        return self.call("sendDocument", {"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML",
                                          "reply_parameters": self.reply_to(reply_to)},
                         files={"document": (filename, content)}, timeout=120)

    def set_buttons(self, message_id, rows):
        try:
            self.call("editMessageReplyMarkup", {"chat_id": self.chat_id, "message_id": message_id,
                                                 "reply_markup": self.keyboard(rows)})
        except TelegramError as exc:
            if "not modified" not in str(exc):
                log(f"[TG] buton güncellenemedi: {exc}")

    def answer_callback(self, callback_id, text=""):
        try:
            self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
        except TelegramError:
            pass  # cron gecikmesinde sorgu zaman aşımına uğramış olabilir; önemli değil


def download(url: str, max_mb: int = 20) -> bytes:
    r = requests.get(url, headers=rf.HTTP_HEADERS, timeout=40, stream=True)
    r.raise_for_status()
    data = b""
    for chunk in r.iter_content(65536):
        data += chunk
        if len(data) > max_mb * 1024 * 1024:
            raise ValueError("görsel çok büyük")
    return data


def esc(text) -> str:
    return html.escape(str(text or ""), quote=False)


def pre(text) -> str:
    return f"<pre>{esc(text)}</pre>"


# --------------------------------------------------------------------------- #
# Haber kartı (RSS -> grup)
# --------------------------------------------------------------------------- #
BTN_PREPARE = ("📝 Haberi Hazırla", "p:{id}")
BTN_SKIP = ("⏭ Pas Geç", "s:{id}")


def card_buttons(news_id: str):
    return Telegram.keyboard([[(BTN_PREPARE[0], BTN_PREPARE[1].format(id=news_id)),
                               (BTN_SKIP[0], BTN_SKIP[1].format(id=news_id))]])


def send_card(tg: Telegram, item: rf.NewsItem, tri: dict) -> int:
    title = tri.get("baslik_tr") or item.title
    game = f"\n🎮 {esc(tri['oyun'])}" if tri.get("oyun") else ""
    caption = (
        f"<b>{esc(title)}</b>\n\n{esc(tri.get('ozet_tr') or item.summary[:250])}{game}\n\n"
        f"📰 <b>Kaynak:</b> {esc(item.source)} · <a href=\"{esc(item.url)}\">habere git</a>"
    )[:1020]
    markup = card_buttons(item.id)
    image = item.image
    if not image:
        try:
            image = cg.fetch_article(item.url).get("image")
        except Exception:
            image = None
    if image:
        try:
            return tg.send_photo(image, caption, markup)["message_id"]
        except Exception as exc:
            log(f"[TG] görsel gönderilemedi ({exc}), metin olarak gönderiliyor")
    return tg.send_text(caption, markup=markup, preview=True)["message_id"]


# --------------------------------------------------------------------------- #
# İçerik paketi ([Haberi Hazırla] veya yapıştırılan link)
# --------------------------------------------------------------------------- #
def chunk_text(text: str, size: int = 3500) -> list[str]:
    parts, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 > size and cur:
            parts.append(cur.strip())
            cur = ""
        cur += para + "\n\n"
    if cur.strip():
        parts.append(cur.strip())
    return parts or [""]


def deliver_package(tg: Telegram, ai, url: str, source: str | None = None, fallback: dict | None = None,
                    reply_to: int | None = None, feeds: list[dict] | None = None) -> dict:
    if not fallback and feeds:  # manuel link: site engellerse RSS'te aynı haberi ara
        try:
            article = cg.fetch_article(url)
            if len(article["text"]) < 300:
                raise ValueError("kısa metin")
        except Exception:
            hit = rf.rss_lookup(url, feeds)
            if hit:
                fallback, source = hit.to_dict(), source or hit.source
    article = cg.fetch_article_safe(url, fallback)
    source = source or cg.source_name_from_url(article["url"], article.get("site_name"))
    pkg = cg.generate_package(ai, article, source)
    if article.get("limited"):
        tg.send_text("ℹ️ Kaynak site tam metne erişimi engelledi; içerik <b>yalnızca RSS özetinden</b> üretildi. "
                     "Yayımlamadan önce kaynağı kontrol edip metni genişletin.", reply_to=reply_to)

    # 0) Yüksek çözünürlüklü kapak görseli (sıkıştırılmasın diye dosya olarak)
    if article.get("image"):
        try:
            img = download(article["image"])
            ext = re.search(r"\.(jpe?g|png|webp|gif)(?:$|\?)", article["image"], re.I)
            tg.send_document(img, f"paralaks-kapak.{ext.group(1).lower() if ext else 'jpg'}",
                             "🖼 <b>Kapak görseli</b> (orijinal çözünürlük)", reply_to=reply_to)
        except Exception as exc:
            log(f"[PKG] görsel indirilemedi ({exc}), Telegram'ın URL'den çekmesi deneniyor")
            try:
                tg.send_photo(article["image"], f"🖼 <b>Kapak görseli</b> — orijinali: {esc(article['image'])}",
                              reply_to=reply_to)
            except Exception:
                tg.send_text(f"🖼 Kapak görseli indirilemedi, link: {esc(article['image'])}", reply_to=reply_to)

    # A) Web sitesi admin paneli
    head = (
        "🅰️ <b>WEB SİTESİ — ADMİN PANELİ</b>\n\n"
        f"<b>Haber Başlığı</b>\n{pre(pkg['baslik'])}\n"
        f"<b>Kısa Özet</b>\n{pre(pkg.get('kisa_ozet', ''))}"
    )
    tg.send_text(head, reply_to=reply_to)
    body_parts = chunk_text(pkg["ana_metin"])
    for n, part in enumerate(body_parts, 1):
        label = "Ana Haber Metni" + (f" ({n}/{len(body_parts)})" if len(body_parts) > 1 else "")
        tg.send_text(f"<b>{label}</b>\n{pre(part)}", reply_to=reply_to)
    k = pkg.get("kunye")
    if k:
        kunye = (
            f"Geliştirici: {k.get('gelistirici') or 'Belirtilmedi'}\n"
            f"Yayıncı: {k.get('yayinci') or 'Belirtilmedi'}\n"
            f"Çıkış Tarihi: {k.get('cikis_tarihi') or 'Duyurulmadı'}\n"
            f"Platformlar: {k.get('platformlar') or 'Belirtilmedi'}\n"
            f"Tür: {k.get('tur') or 'Belirtilmedi'}\n"
            f"Türkçe: {k.get('turkce') or 'Yok'}"
        )
        tg.send_text(f"<b>Oyun Künyesi</b>\n{pre(kunye)}\n<i>⚠️ Künye AI tarafından üretildi; "
                     f"yayımlamadan önce doğrulayın.</i>", reply_to=reply_to)

    # B) X (Twitter)
    x = pkg["x_post"]
    kind = "Kampanya / indirim (link eklendi)" if pkg.get("haber_tipi") == "kampanya" \
        else "Standart (başta emoji için boşluk var)"
    tg.send_text(f"🅱️ <b>X (TWITTER) PAYLAŞIMI</b> — {kind}\n{pre(x)}\n"
                 f"<i>{cg.x_length(x)}/{cg.X_LIMIT} karakter · 🔗 {esc(source)}: {esc(article['url'])}</i>",
                 reply_to=reply_to)

    # C) Instagram özeti (@paralaksonline)
    ig = pkg.get("instagram") or []
    if len(ig) >= 3:
        tg.send_text("📸 <b>INSTAGRAM ÖZETİ</b> — @paralaksonline\n" + pre("\n".join(f"▪️ {m}" for m in ig)),
                     reply_to=reply_to)
    else:
        log(f"[PKG] Instagram özeti eksik geldi ({len(ig)} madde), gönderilmedi")
    return pkg


# --------------------------------------------------------------------------- #
# Telegram güncellemelerini işleme
# --------------------------------------------------------------------------- #
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)


def handle_callback(tg: Telegram, ai, cfg, state, cq: dict) -> None:
    msg = cq.get("message") or {}
    if str((msg.get("chat") or {}).get("id")) != tg.chat_id:
        return
    data = cq.get("data") or ""
    action, _, news_id = data.partition(":")
    mid = msg.get("message_id")
    if action == "x":  # bilgi butonu
        tg.answer_callback(cq["id"])
        return
    item = state["pending"].get(news_id)
    log(f"[BUTON] {'Hazırla' if action == 'p' else 'Pas Geç' if action == 's' else action}: "
        f"{(item or {}).get('title', news_id)[:80]}")
    if not item:
        tg.answer_callback(cq["id"], "Bu haberin kaydı bulunamadı (süresi dolmuş olabilir).")
        return
    if action == "s":
        tg.answer_callback(cq["id"], "Pas geçildi")
        tg.set_buttons(mid, [[("⏭ Pas geçildi", "x:0")]])
        state["pending"].pop(news_id, None)
        return
    if action == "p":
        tg.answer_callback(cq["id"], "Hazırlanıyor…")
        if tg.limited:  # boşa AI çağrısı yapma; buton sonraki çalıştırmada tekrar işlenecek
            return
        tg.set_buttons(mid, [[("⏳ Hazırlanıyor…", "x:0")]])
        try:
            deliver_package(tg, ai, item["url"], item.get("source"), fallback=item, reply_to=mid)
            tg.set_buttons(mid, [[("✅ Hazırlandı", "x:0")]])
            state["pending"].pop(news_id, None)
        except Exception as exc:
            log(f"[PKG] hata: {exc}")
            tg.set_buttons(mid, [[("🔁 Tekrar Dene", f"p:{news_id}"), ("⏭ Pas Geç", f"s:{news_id}")]])
            tg.send_text(f"⚠️ Haber hazırlanamadı: <code>{esc(str(exc)[:300])}</code>", reply_to=mid)


def handle_message(tg: Telegram, ai, cfg, state, msg: dict) -> None:
    if str((msg.get("chat") or {}).get("id")) != tg.chat_id:
        return
    sender = msg.get("from") or {}
    if sender.get("is_bot"):
        return
    allowed = [str(u) for u in cfg.get("allowed_user_ids") or []]
    if allowed and str(sender.get("id")) not in allowed:
        return
    text = msg.get("text") or msg.get("caption") or ""
    if text.strip().lower().startswith("/ping"):
        tg.send_text("🏓 PARALAKS haber masası çalışıyor.", reply_to=msg["message_id"])
        return
    urls = []
    for ent in (msg.get("entities") or []) + (msg.get("caption_entities") or []):
        if ent.get("type") == "text_link" and ent.get("url"):
            urls.append(ent["url"])
    urls += URL_RE.findall(text)
    seen_local = set()
    for url in urls:
        url = url.rstrip(").,;!?»”")
        if url in seen_local or "t.me/" in url:
            continue
        seen_local.add(url)
        if len(seen_local) > 3:
            break
        log(f"[LINK] manuel link: {url}")
        try:
            deliver_package(tg, ai, url, reply_to=msg["message_id"], feeds=cfg["feeds"])
            state["seen"][rf.item_id(url)] = time.time()
        except Exception as exc:
            log(f"[LINK] hata: {exc}")
            tg.send_text(f"⚠️ Link işlenemedi: <code>{esc(str(exc)[:300])}</code>", reply_to=msg["message_id"])


def process_updates(tg: Telegram, ai, cfg, state, wait: int = 0) -> int:
    updates = tg.call("getUpdates", {
        "offset": state.get("telegram_offset", 0) or None,
        "timeout": wait,
        "allowed_updates": ["message", "callback_query"],
    }, timeout=wait + 20)
    for upd in updates:
        state["telegram_offset"] = upd["update_id"] + 1
        try:
            if "callback_query" in upd:
                handle_callback(tg, ai, cfg, state, upd["callback_query"])
            elif "message" in upd:
                handle_message(tg, ai, cfg, state, upd["message"])
        except Exception as exc:
            log(f"[TG] güncelleme işlenemedi: {exc}")
        if tg.limited:
            # Hız sınırı yüzünden yarım kaldı: bu güncellemeyi (buton/link) sonraki çalıştırmada tekrar işle
            state["telegram_offset"] = upd["update_id"]
            log("[TG] hız sınırı nedeniyle kalan güncellemeler sonraki çalıştırmaya bırakıldı")
            break
    return len(updates)


# --------------------------------------------------------------------------- #
# RSS döngüsü
# --------------------------------------------------------------------------- #
def run_rss(tg: Telegram, ai, cfg, state) -> None:
    if tg.limited:
        log("[RSS] Telegram hız sınırı aktif; RSS turu atlandı (haberler sonraki çalıştırmada gönderilecek)")
        return
    items = rf.fetch_all(cfg["feeds"], cfg["max_age_hours"], log)
    now = time.time()
    fresh, by_id = [], {}
    for it in items:
        if it.id in state["seen"] or it.id in by_id:
            continue
        by_id[it.id] = it
        ok, reason = rf.rule_filter(it, cfg.get("extra_blocklist"))
        if not ok:
            state["seen"][it.id] = now
            log(f"[FİLTRE] ✗ {it.source}: {it.title[:80]} — {reason}")
            continue
        fresh.append(it)

    fresh.sort(key=lambda i: i.published)
    limit = cfg["max_items_per_run"]
    if len(fresh) > limit:  # birikme varsa en yenileri al, eskileri sessizce geç
        for old in fresh[:-limit]:
            state["seen"][old.id] = now
        log(f"[RSS] {len(fresh) - limit} eski haber limit nedeniyle atlandı")
        fresh = fresh[-limit:]
    if not fresh:
        log("[RSS] yeni haber yok")
        return

    batch = cfg["ai_batch_size"]
    for start in range(0, len(fresh), batch):
        if tg.limited:  # kalan haberler "görülmedi" kalır, sonraki çalıştırmada tekrar denenir
            log("[RSS] Telegram hız sınırı aktif; kalan haberler sonraki çalıştırmaya bırakıldı")
            break
        group = fresh[start:start + batch]
        try:
            cutoff = time.time() - cfg["dedupe_hours"] * 3600
            recent = [f"[{r.get('source')}] {r.get('title')}" for r in state["sent"] if r.get("ts", 0) >= cutoff]
            results = cg.triage(ai, group, recent)
        except Exception as exc:
            log(f"[AI] ön eleme başarısız, bu grup sonraki çalıştırmada denenecek: {exc}")
            continue
        for it, tri in zip(group, results):
            state["seen"][it.id] = time.time()
            if not tri.get("oyun_haberi"):
                log(f"[BAĞLAM] ✗ {it.source}: {it.title[:80]} — AI: oyun haberi değil")
                continue
            if tri.get("tekrar"):
                log(f"[KOPYA] ✗ {it.source}: {it.title[:70]} — AI: aynı gelişme daha önce gönderildi")
                continue
            dup = rf.find_duplicate(tri.get("oyun", ""), tri.get("olay", ""), it.title, state["sent"],
                                    cfg["dedupe_hours"], cfg.get("dedupe_same_game", False))
            if dup:
                log(f"[KOPYA] ✗ {it.source}: {it.title[:70]} — {dup.get('source')} zaten attı")
                continue
            try:
                mid = send_card(tg, it, tri)
            except Exception as exc:
                log(f"[TG] kart gönderilemedi: {exc}")
                state["seen"].pop(it.id, None)
                continue
            log(f"[GÖNDERİLDİ] {it.source}: {it.title[:80]}")
            state["sent"].append({"id": it.id, "ts": time.time(), "source": it.source, "title": it.title,
                                  "game": tri.get("oyun", ""), "event": tri.get("olay", ""), "msg": mid})
            state["pending"][it.id] = {"url": it.url, "source": it.source, "title": it.title,
                                       "summary": it.summary, "content": it.content[:4000],
                                       "image": it.image, "ts": time.time()}
            time.sleep(1.2)  # Telegram grup limiti: ~20 mesaj/dk
        if start + batch < len(fresh):
            time.sleep(cfg.get("ai_pause_seconds", 5))


# --------------------------------------------------------------------------- #
# Modlar
# --------------------------------------------------------------------------- #
def dry_run(cfg) -> None:
    items = rf.fetch_all(cfg["feeds"], cfg["max_age_hours"], log)
    passed = 0
    for it in sorted(items, key=lambda i: i.source):
        ok, reason = rf.rule_filter(it, cfg.get("extra_blocklist"))
        passed += ok
        print(f"  {'✓' if ok else '✗'} [{it.source}] {it.title[:90]}  ({reason}){'' if it.image else '  [görsel yok]'}")
    print(f"\nToplam {len(items)} haber, kural filtresinden {passed} tanesi geçti (AI bağlam filtresi ayrıca çalışır).")


def run_test(tg: Telegram, ai, cfg) -> None:
    me = tg.call("getMe")
    log(f"[TEST] Telegram bot: @{me['username']} ✓")
    hook = tg.call("getWebhookInfo")
    if hook.get("url"):
        tg.call("deleteWebhook")
        log("[TEST] Aktif webhook kaldırıldı (getUpdates için gerekli) ✓")
    chat = tg.call("getChat", {"chat_id": tg.chat_id})
    log(f"[TEST] Grup: {chat.get('title') or chat.get('id')} ✓")
    if chat.get("type") in ("group", "supergroup") and not me.get("can_read_all_group_messages"):
        log("[TEST] ⚠️ Gizlilik modu AÇIK: bot gruba yapıştırdığınız linkleri göremez. "
            "BotFather -> /setprivacy -> Disable yapın ya da botu grupta yönetici yapın.")
    res = ai.generate_json('Yalnızca {"durum": "..."} biçiminde JSON döndür.',
                           "Türkçe olarak tek kelimeyle 'hazır' yaz.", max_tokens=200)
    log(f"[TEST] AI ({ai.name}): {res} ✓")
    tg.send_text("✅ <b>PARALAKS Haber Masası bağlandı.</b>\n"
                 f"AI sağlayıcı: <code>{esc(ai.name)}</code>\n"
                 "Gruba bir haber linki yapıştırarak üretim hattını deneyebilirsiniz.")
    log("[TEST] Gruba test mesajı gönderildi ✓")


def main() -> int:
    ap = argparse.ArgumentParser(description="PARALAKS Telegram Oyun Haberi Masası")
    ap.add_argument("--test", action="store_true", help="Telegram + AI bağlantı testi")
    ap.add_argument("--dry-run", action="store_true", help="Sadece RSS + kural filtresi, hiçbir şey gönderme")
    ap.add_argument("--url", help="Verilen haber linki için içerik paketi üret")
    ap.add_argument("--no-rss", action="store_true", help="RSS taramasını atla, sadece Telegram'ı işle")
    ap.add_argument("--listen", type=int, default=None, help="RSS sonrası saniye cinsinden dinleme süresi")
    args = ap.parse_args()

    cfg = load_config()

    if args.dry_run:
        dry_run(cfg)
        return 0

    missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not cfg.get(k)]
    if missing:
        log(f"[HATA] Eksik ayar: {', '.join(missing)} (config.json, .env veya GitHub Secrets)")
        return 1
    tg = Telegram(cfg["TELEGRAM_BOT_TOKEN"], cfg["TELEGRAM_CHAT_ID"])
    ai = cg.get_ai(cfg)
    log(f"[BAŞLA] AI sağlayıcı: {ai.name} ({getattr(ai, 'model', '')})")

    if args.test:
        run_test(tg, ai, cfg)
        return 0

    state = load_state()
    try:
        if args.url:
            deliver_package(tg, ai, args.url, feeds=cfg["feeds"])
            return 0

        failures = 0
        # Bir aşama (ör. Telegram'a geçici erişim sorunu) çökerse diğer aşamalar yine çalışsın
        try:
            n = process_updates(tg, ai, cfg, state)
            log(f"[TG] {n} güncelleme işlendi")
        except TelegramError as exc:
            # Telegram hız sınırı veya erişilemiyor: bu çalıştırmada Telegram'a hiç dokunma.
            # Haberler ve buton basmaları kaybolmaz, sonraki çalıştırmada işlenir. Hata e-postası gönderilmez.
            tg.limited = True
            log(f"[TG] Telegram şu an kullanılamıyor ({exc}); bu çalıştırma Telegram'a başka istek göndermeyecek")
        except Exception as exc:
            failures += 1
            log(f"[HATA] Telegram güncellemeleri işlenemedi: {exc}")
        save_state(state, cfg)

        if not args.no_rss:
            try:
                run_rss(tg, ai, cfg, state)
            except Exception as exc:
                failures += 1
                log(f"[HATA] RSS turu tamamlanamadı: {exc}")
            save_state(state, cfg)

        listen = args.listen if args.listen is not None else int(cfg.get("LISTEN_SECONDS") or 0)
        deadline = time.time() + listen
        while time.time() < deadline - 5 and not tg.limited:
            wait = int(min(50, deadline - time.time() - 5))
            if wait < 1:
                break
            try:
                if process_updates(tg, ai, cfg, state, wait=wait):
                    save_state(state, cfg)
            except Exception as exc:
                log(f"[HATA] dinleme sırasında: {exc}")
                time.sleep(5)
    finally:
        save_state(state, cfg)
    log("[BİTTİ]" if not failures else f"[BİTTİ] {failures} aşama hatayla sonuçlandı")
    # Her iki aşama da çöktüyse (ör. token geçersiz) GitHub'ın hata e-postası gelsin
    return 1 if failures >= 2 else 0


if __name__ == "__main__":
    sys.exit(main())
