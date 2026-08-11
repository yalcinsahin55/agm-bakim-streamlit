# AGM Motor Bakım Merkezi — Streamlit Sürümü

Bu sürüm, ayrı bir sunucu (Render/Docker) kurmaya gerek kalmadan,
tamamen Streamlit Community Cloud üzerinden — sadece telefon
tarayıcısıyla — canlıya alınabilir. Tek ihtiyacın: bir GitHub hesabı
(zaten var), bir MongoDB Atlas hesabı (ücretsiz), ve bir Streamlit
hesabı (ücretsiz).

## Neden daha kolay?

Önceki (FastAPI) sürümde şu adımlar vardı: GitHub → Codespaces →
Render (Docker build, ortam değişkenleri) → MongoDB Atlas. Bu sürümde
Render/Docker adımı tamamen kalkıyor: kodu GitHub'a atıyorsun,
share.streamlit.io'da "New app" diyip deponu seçiyorsun, birkaç saniye
sonra uygulaman canlı oluyor.

## Adımlar (özet — sohbette tek tek anlatılacak)

1. **GitHub'da yeni bir depo (repo) oluştur** — örn. `agm-bakim-streamlit`
2. **Bu klasördeki dosyaları o depoya yükle** (Codespaces ile, aynı önceki yöntem)
3. **MongoDB Atlas'ta ücretsiz veritabanı oluştur** (M0 cluster, kullanıcı adı/şifre, `0.0.0.0/0` erişim izni)
4. **share.streamlit.io**'da GitHub hesabınla giriş yap, "New app" → deponu seç → `app.py` dosyasını göster
5. **Secrets** (Gizli Bilgiler) kısmına `MONGO_URI` bağlantı adresini yapıştır
6. **Deploy** — birkaç dakika içinde uygulaman `https://xxxxx.streamlit.app` adresinde canlı olur

## Dosyalar

```
streamlit_app/
├── app.py                          # Tüm uygulama (tek dosya)
├── requirements.txt                # Gerekli Python paketleri
├── seed_data.json                  # V10.xlsx'ten çıkarılmış gerçek veri (ilk açılışta otomatik yüklenir)
├── .streamlit/secrets.toml.example # MONGO_URI için örnek — GERÇEK değerleri
│                                    # Streamlit Cloud panelinden gireceksin, bu dosyayı
│                                    # değiştirip yüklemene gerek yok
└── .gitignore
```

## İlk kullanıcı ve roller

Uygulama ilk açıldığında "Sistemde henüz kullanıcı yok" mesajını
görürsün — orada oluşturduğun ilk hesap otomatik **yönetici** olur.
Bundan sonraki kişiler "Yeni Hesap (Teknisyen)" sekmesinden kendi
hesaplarını açabilir (varsayılan rol: teknisyen); bir yönetici,
**Kullanıcılar** sayfasından herkesin rolünü değiştirebilir
(yönetici / planlamacı / teknisyen / görüntüleyici).

## Veriler nerede saklanıyor?

Tüm motor, bakım ve kullanıcı verisi MongoDB Atlas'taki ücretsiz
veritabanında saklanıyor — Streamlit uygulaması yeniden başlasa
(örn. uzun süre kullanılmayıp uyusa) bile veriler kaybolmaz, çünkü
veritabanı ayrı bir serviste duruyor. Fotoğraflar da (küçültülmüş
biçimde) veritabanında saklanıyor, ayrı bir depolama servisi gerekmez.

## Sorun mu yaşıyorsun?

- **Uygulama açılırken hata veriyor:** Streamlit Cloud'daki
  **Manage app → Logs** kısmına bak; genelde `MONGO_URI` eksik/yanlış
  girilmiş olur.
- **Veritabanına bağlanamıyor:** Atlas'taki **Network Access**
  ayarında `0.0.0.0/0` eklendiğinden emin ol.
- **Fotoğraf yüklerken hata:** Çok büyük bir fotoğraf seçilmiş
  olabilir; uygulama otomatik küçültüyor ama çok büyük dosyalarda
  (30MB+) zaman aşımı yaşanabilir, daha küçük bir fotoğraf deneyin.
