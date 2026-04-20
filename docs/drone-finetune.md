# Drone-Özel YOLO Fine-Tune Rehberi

**Amaç:** COCO "airplane/bird" etiketlerini `drone/helicopter/multirotor`
ile değiştir. 2-3 saat, Colab T4 GPU **ücretsiz**.

## Gerekli Dosyalar

- [roboflow-drone-finetune.ipynb](./roboflow-drone-finetune.ipynb) — Colab notebook (aşağıda)
- Roboflow "Drone Detection" dataset (ücretsiz hesap)
- Google hesabı (Colab + Drive)

## Adım 1: Dataset İndir

1. https://universe.roboflow.com/browse?q=drone açıyoruz
2. "Drone Detection Dataset" seç (en popüler, ~5000 görüntü)
3. Format: **YOLOv8 PyTorch** seç → `Download zip` veya curl URL
4. 3-4 dataset birleştirmeyi düşün (farklı açı + ışık çeşitliliği)

## Adım 2: Colab Notebook

Aşağıdaki hücreleri sırayla çalıştır (dosya: `roboflow-drone-finetune.ipynb`):

```python
# Hücre 1 — GPU kontrol
import torch
print(torch.cuda.is_available(), torch.cuda.get_device_name(0))
# Çıktı: True, Tesla T4

# Hücre 2 — ultralytics kur
!pip install ultralytics roboflow

# Hücre 3 — Dataset indir (Roboflow API key gerekli)
from roboflow import Roboflow
rf = Roboflow(api_key="YOUR_API_KEY")
project = rf.workspace("DATASET_OWNER").project("drone-detection")
dataset = project.version(1).download("yolov8")

# Hücre 4 — Fine-tune
from ultralytics import YOLO
model = YOLO("yolov8n.pt")
results = model.train(
    data=f"{dataset.location}/data.yaml",
    epochs=50,
    imgsz=640,
    batch=16,
    device=0,   # T4 GPU
    project="nizam-drone",
    name="v1",
    patience=10,
    optimizer="AdamW",
)

# Hücre 5 — Değerlendirme
metrics = model.val()
print(f"mAP50: {metrics.box.map50:.3f}")
print(f"mAP50-95: {metrics.box.map:.3f}")
# Hedef: mAP50 > 0.80

# Hücre 6 — Export + indir
model.export(format="onnx", opset=17, imgsz=640)
# nizam-drone/v1/weights/best.pt
# nizam-drone/v1/weights/best.onnx

# Drive'a yedek
from google.colab import drive
drive.mount('/content/drive')
!cp nizam-drone/v1/weights/best.pt /content/drive/MyDrive/yolov8n-drone-v1.pt
!cp nizam-drone/v1/weights/best.onnx /content/drive/MyDrive/yolov8n-drone-v1.onnx
```

## Adım 3: NIZAM'a Entegrasyon

Eğitilmiş model dosyasını repo'ya al:

```bash
# Drive'dan indir, ai/models/'a koy
cp ~/Downloads/yolov8n-drone-v1.pt ai/models/
cp ~/Downloads/yolov8n-drone-v1.onnx ai/models/

# Servisi yeni modelle başlat
python -m services.detectors.camera.yolo_service \
    --model ai/models/yolov8n-drone-v1.pt \
    --source 0 --sensor-id cam-01

# GPU varsa ONNX hızlı:
python -m services.detectors.camera.yolo_service \
    --model ai/models/yolov8n-drone-v1.onnx \
    --source 0 --sensor-id cam-01
```

## Beklenen Sonuç

| Metrik | COCO | Fine-tuned |
|---|---|---|
| mAP50 drone | ~0.35 (airplane ← drone) | **>0.80** |
| mAP50-95 | ~0.20 | **>0.55** |
| False positive (sokak) | yüksek | düşük |
| Sınıflar | 80 COCO | drone/helicopter/multirotor/bird |

## Dataset Kaynakları

Roboflow Universe'de etiketli dataset'ler:

| Dataset | Görüntü | Sınıf |
|---|---|---|
| Drone Detection (popüler) | ~4500 | drone |
| DJI Drone Detection | ~2000 | dji-drone |
| Aerial Vehicle Classification | ~10000 | drone, helicopter, plane |
| Counter-Drone | ~3000 | drone, bird, plane, kite |

Birden fazla dataset birleştir (YOLOv8 `data.yaml` aynı sınıflarla).

## Türkiye-Özel Dataset (gelecek)

Üretim için:
- Kendi saha video'larından 500-1000 frame topla
- CVAT veya Label Studio ile etiketle
- Roboflow dataset'iyle birleştir
- Her 3 ayda bir re-train (drift önleme)

## Sorun Giderme

**"CUDA out of memory":** `batch=8` yap (16 yerine).

**"mAP50 düşük (<0.6)":** Epoch sayısını artır (100), learning rate düşür
(0.01 → 0.001), dataset kalitesini kontrol et.

**"Inference latency yüksek":** ONNX export kullan (2-3x hızlı), TensorRT
varsa engine export (5-10x hızlı — `scripts/export_yolo.sh`).
