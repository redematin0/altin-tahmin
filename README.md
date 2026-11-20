# Altın Tahmin

Kişisel gram altın takip, tahmin ve portföy uygulaması.

- **Site:** https://redematin0.github.io/altin-tahmin/
- **Sunucu:** `.github/workflows/altin.yml` her ~20 dakikada `scripts/collect.py`'yi çalıştırır:
  fiyat geçmişi (Yahoo), Kapalıçarşı fiyatları, haberler, ekonomik takvim, yöntem seçimi
  (geriye dönük test), tahmin, tahmin günlüğü ve Telegram botu.
- **Hafıza:** `state` dalında tutulur (her çalışmada tek commit olarak üzerine yazılır).
- **Gizli anahtarlar (Settings → Secrets → Actions):** `TELEGRAM_BOT_TOKEN` (bildirimler için),
  isteğe bağlı `TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY` (haberleri Claude ile puanlamak için).
- Yerel test: `OFFLINE=1 DATA_DIR=/tmp/d python scripts/collect.py`

Yatırım tavsiyesi değildir.
