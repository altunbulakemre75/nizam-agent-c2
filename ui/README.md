# NIZAM 3D Operatör UI

CesiumJS + deck.gl + TypeScript.

## İlk Kurulum

```bash
cd ui
npm install        # ~2-3 dk, 200MB node_modules
```

## Geliştirme

```bash
npm run dev        # http://127.0.0.1:5173
```

Gateway 8200'de çalışıyor olmalı; Vite `/ws` → `ws://localhost:8200` proxy'ler.

## Prod Build

```bash
npm run build      # dist/ çıktısı (minified + tree-shaken)
npm run preview    # dist'i preview et
```

## Kaynaklar

- [src/main.ts](src/main.ts) — Cesium viewer setup + WebSocket
- [src/track_renderer.ts](src/track_renderer.ts) — Track entities (point + trail + label)
- [src/deck_overlay.ts](src/deck_overlay.ts) — deck.gl PathLayer + ScatterplotLayer

## Sorun Giderme

### "Cannot find module cesium"
```bash
rm -rf node_modules package-lock.json
npm install
```

### WebSocket bağlanmıyor
Gateway'in çalıştığından emin ol:
```bash
curl http://localhost:8200/health     # {"status":"ok"}
```

### Cesium 3D globe siyah
Ion access token yok (boş bırakıldı). Cesium otomatik OSM base layer kullanır.
Bulut görmüyorsan tarayıcı WebGL desteğini kontrol et: about:gpu
