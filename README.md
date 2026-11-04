# Notlio Runner

Notlio uygulamasindaki kodlari (Python, C, C++, Java, Go, Rust, PHP, Ruby, Perl, Lua,
Bash, SQL, JavaScript, TypeScript, C#, Dart...) calistiran kucuk sunucu.
Piston ile ayni arayuzu kullanir; uygulamada sadece sunucu adresini degistirmen yeterli.

## Kurulum (bir kereye mahsus, ~10 dk)

1. GitHub'da **yeni bir depo** ac (ornek: `notlio-runner`). Uygulamanin deposundan ayri olsun.
2. Bu klasordeki dosyalari (Dockerfile, server.py, render.yaml, README.md ...) o depoya yukle.
3. https://render.com > **New +** > **Blueprint** > bu depoyu sec > **Apply**.
   (Blueprint yerine **New > Web Service > Docker** da olur: plan Free, Health Check Path `/`.)
4. Ilk derleme 10-15 dakika surebilir. Bittiginde adresin gorunur:
   `https://notlio-runner-xxxx.onrender.com`
5. Render > servis > **Environment** > `RUNNER_KEY` degerini kopyala (gizli anahtar).
6. Notlio > **Ayarlar > Kod calistirma > sunucu adresi** alanina yaz:

   `https://notlio-runner-xxxx.onrender.com/api/v2/execute?key=BURAYA_ANAHTAR`

## Test

```
curl -s "https://ADRESIN.onrender.com/api/v2/execute?key=ANAHTAR" \
  -H "Content-Type: application/json" \
  -d '{"language":"python","files":[{"name":"main.py","content":"print(42)"}]}'
```

`https://ADRESIN.onrender.com/` acilirsa yuklu dillerin listesini gorursun.

## Bilmen gerekenler

- **Ucretsiz plan uyur:** 15 dk kullanilmazsa uyur, ilk istek ~1 dk surer. Uygulama ilk calistirmada bekler.
- **Ucretsiz plan 512 MB RAM:** buyuk programlar olur. Kotlin icin Environment'a `INSTALL_KOTLIN=1` ekleyip
  daha yuksek bir plan kullan. Swift desteklenmez (cok buyuk).
- **Guvenlik:** Kod, yetkisiz kullanici (uid 10001) ile, sure/bellek/cikti sinirlariyla calisir ve
  sunucunun gizli anahtarina erisemez. Ancak sunucuda **internet erisimi engellenemez**. Adresi/anahtari kimseyle paylasma.
  Anahtar sizarsa Render'da `RUNNER_KEY` degerini degistir.
- Ayarlar (Environment): `RUN_TIMEOUT` (sn, varsayilan 10), `COMPILE_TIMEOUT` (40), `MAX_PARALLEL` (2),
  `MAX_OUTPUT` (bayt, 262144).
