# PARALAKS Haber Masası (Telegram Bot)

GitHub Actions üzerinde periyodik çalışan, oyun haberlerini tarayıp Telegram grubuna atan ve
`[Haberi Hazırla]` ile PARALAKS admin paneli + X metni üreten bot.

## Dosyalar
| Dosya | Görev |
|---|---|
| `bot.py` | Ana akış: Telegram güncellemeleri (butonlar, yapıştırılan linkler), RSS döngüsü, çıktıların gönderimi |
| `rss_fetcher.py` | RSS çekme, kural tabanlı filtre (film/dizi/çizgi roman/donanım reklamı/rehber), kopya kontrolü |
| `content_generator.py` | Gemini / Claude sağlayıcı katmanı, AI bağlam filtresi + Türkçe özet, tam metin + görsel çekme, içerik paketi |
| `state.json` | Görülen haberler, gönderilenler, bekleyen kartlar, Telegram ofseti (workflow commit'ler). 14 günden eski kayıtlar her kayıtta otomatik silinir (`state_retention_days`) |
| `config.example.json` | Tüm ayarlar ve varsayılanlar. Yerelde `config.json` olarak kopyalayın (git'e girmez) |
| `.github/workflows/rss_cron.yml` | Zamanlayıcı (varsayılan 30 dk) |

## `[Haberi Hazırla]` çıktısı
Kapak görseli (dosya) → 🅰️ Admin paneli (başlık, kısa özet, ana metin, künye) → 🅱️ X metni → 📸 Instagram özeti (3-4 madde, @paralaksonline)

## Komutlar
```
python bot.py --dry-run     # sadece RSS + filtre; hiçbir şey gönderilmez
python bot.py --test        # Telegram + AI bağlantı testi, gruba test mesajı
python bot.py --url <link>  # tek link için içerik paketi
python bot.py               # tam döngü
python bot.py --listen 300  # tam döngü + 5 dk buton dinleme
```

## Ayar önceliği
Ortam değişkenleri / GitHub Secrets  >  `.env`  >  `config.json`  >  `config.example.json`

`AI_PROVIDER` = `gemini` veya `claude`.

## Bilinen sınırlar
- Butonlar bir sonraki çalıştırmada işlenir (cron aralığı + GitHub gecikmesi). Daha hızlı tepki için
  `LISTEN_SECONDS` değişkenini artırın (Actions dakikası harcar).
- Gematsu ve VGC makale sayfaları Cloudflare ile otomatik erişimi engelliyor; bu sitelerde içerik RSS özetinden
  üretilir ve bot bunu mesajda belirtir.
- Künye alanları AI üretimidir; yayımlamadan önce doğrulayın.
