#!/usr/bin/env bash
# NIZAM YOLO model export — PyTorch → ONNX → TensorRT
#
# Kullanım:
#   bash scripts/export_yolo.sh yolov8n.pt
#   bash scripts/export_yolo.sh ai/models/yolov8n-drone.pt
#
# Sonuçlar:
#   yolov8n.onnx     (CPU/GPU, ONNX Runtime ile 2-3x hızlı)
#   yolov8n.engine   (TensorRT, NVIDIA GPU, 5-10x hızlı — CUDA gerek)

set -euo pipefail
MODEL="${1:-yolov8n.pt}"

if [[ ! -f "$MODEL" ]]; then
  echo "Model bulunamadı: $MODEL"
  exit 1
fi

echo "→ ONNX export..."
python -c "
from ultralytics import YOLO
m = YOLO('$MODEL')
m.export(format='onnx', opset=17, simplify=True, dynamic=False, imgsz=640)
print('✓ ONNX export tamam')
"

if command -v nvidia-smi &>/dev/null; then
  echo "→ TensorRT export (CUDA tespit edildi)..."
  python -c "
from ultralytics import YOLO
m = YOLO('$MODEL')
m.export(format='engine', half=True, imgsz=640)  # FP16
print('✓ TensorRT export tamam')
" || echo "TensorRT export başarısız — CUDA toolkit + TensorRT kurulu mu?"
else
  echo "⊗ CUDA yok, TensorRT export atlandı (ONNX yeterli)"
fi

echo ""
echo "Kullanım:"
echo "  python -m services.detectors.camera.yolo_service --model ${MODEL%.pt}.onnx"
echo "  python -m services.detectors.camera.yolo_service --model ${MODEL%.pt}.engine"
