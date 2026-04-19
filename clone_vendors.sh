#!/usr/bin/env bash
# NIZAM Vendor Clone — 89 repoyu vendor/ altına shallow clone eder
# Kullanım: bash clone_vendors.sh [area_num]  (örn: "01" sadece CV alanı)

set -e
ROOT="${PWD}/vendor"
mkdir -p "$ROOT"
AREA_FILTER="${1:-all}"

clone() {
  local area="$1" url="$2"
  [[ "$AREA_FILTER" != "all" && "$AREA_FILTER" != "$area" ]] && return
  local name=$(basename "$url" .git)
  local dst="$ROOT/$area/$name"
  if [[ -d "$dst" ]]; then
    echo "  ⭐  $area/$name (zaten var)"
  else
    echo "  ↓  $area/$name"
    git clone --depth 1 --quiet "$url" "$dst" 2>/dev/null || echo "     ✗ başarısız: $url"
  fi
}

echo "─── NIZAM vendor clone başlıyor ───"

# Area 1: Computer Vision
clone 01-cv https://github.com/ultralytics/ultralytics
clone 01-cv https://github.com/mgonzs13/yolo_ros
clone 01-cv https://github.com/roboflow/rf-detr
clone 01-cv https://github.com/roboflow/supervision
clone 01-cv https://github.com/roboflow/trackers
clone 01-cv https://github.com/ifzhang/ByteTrack
clone 01-cv https://github.com/opencv/opencv
clone 01-cv https://github.com/tryolabs/norfair

# Area 2: Robotics
clone 02-robotics https://github.com/ros-navigation/navigation2
clone 02-robotics https://github.com/mavlink/MAVSDK
clone 02-robotics https://github.com/mavlink/mavlink
clone 02-robotics https://github.com/PX4/PX4-Autopilot
clone 02-robotics https://github.com/introlab/rtabmap
clone 02-robotics https://github.com/NVIDIA-ISAAC-ROS/isaac_ros_common
clone 02-robotics https://github.com/NVIDIA-ISAAC-ROS/isaac_ros_visual_slam
clone 02-robotics https://github.com/cvg/nice-slam

# Area 3: AI Agent
clone 03-ai-agent https://github.com/anthropics/claude-agent-sdk-python
clone 03-ai-agent https://github.com/langchain-ai/langgraph
clone 03-ai-agent https://github.com/openai/openai-agents-python
clone 03-ai-agent https://github.com/microsoft/autogen
clone 03-ai-agent https://github.com/microsoft/semantic-kernel
clone 03-ai-agent https://github.com/crewAIInc/crewAI
clone 03-ai-agent https://github.com/run-llama/llama_index
clone 03-ai-agent https://github.com/langgenius/dify

# Area 4: Local LLM
clone 04-local-llm https://github.com/ggerganov/llama.cpp
clone 04-local-llm https://github.com/ollama/ollama
clone 04-local-llm https://github.com/vllm-project/vllm
clone 04-local-llm https://github.com/huggingface/text-generation-inference
clone 04-local-llm https://github.com/sgl-project/sglang
clone 04-local-llm https://github.com/mudler/LocalAI
clone 04-local-llm https://github.com/nomic-ai/gpt4all
clone 04-local-llm https://github.com/LostRuins/koboldcpp
clone 04-local-llm https://github.com/menloresearch/jan

# Area 5: Streaming
clone 05-streaming https://github.com/nats-io/nats-server
clone 05-streaming https://github.com/redpanda-data/redpanda
clone 05-streaming https://github.com/AutoMQ/automq
clone 05-streaming https://github.com/apache/flink
clone 05-streaming https://github.com/apache/pulsar

# Area 6: Vector DB
clone 06-vector-db https://github.com/facebookresearch/faiss
clone 06-vector-db https://github.com/pgvector/pgvector
clone 06-vector-db https://github.com/qdrant/qdrant
clone 06-vector-db https://github.com/milvus-io/milvus
clone 06-vector-db https://github.com/chroma-core/chroma

# Area 7: Monitoring
clone 07-monitoring https://github.com/prometheus/prometheus
clone 07-monitoring https://github.com/grafana/grafana
clone 07-monitoring https://github.com/grafana/loki
clone 07-monitoring https://github.com/grafana/tempo
clone 07-monitoring https://github.com/grafana/mimir
clone 07-monitoring https://github.com/VictoriaMetrics/VictoriaMetrics
clone 07-monitoring https://github.com/SigNoz/signoz
clone 07-monitoring https://github.com/openobserve/openobserve

# Area 8: Deployment
clone 08-deployment https://github.com/portainer/portainer
clone 08-deployment https://github.com/coollabsio/coolify
clone 08-deployment https://github.com/moghtech/komodo
clone 08-deployment https://github.com/basecamp/kamal
clone 08-deployment https://github.com/dokku/dokku
clone 08-deployment https://github.com/dokploy/dokploy
clone 08-deployment https://github.com/caprover/caprover

# Area 9: Sensor Fusion
clone 09-fusion https://github.com/rlabbe/filterpy
clone 09-fusion https://github.com/rlabbe/Kalman-and-Bayesian-Filters-in-Python
clone 09-fusion https://github.com/mzahana/smart_track
clone 09-fusion https://github.com/IacopomC/Sensor-Fusion-3D-Multi-Object-Tracking
clone 09-fusion https://github.com/zhuxuekui3/RAFT

# Area 10: TAK/CoT
clone 10-tak https://github.com/snstac/pytak
clone 10-tak https://github.com/FreeTAKTeam/FreeTakServer
clone 10-tak https://github.com/kdudkov/goatak
clone 10-tak https://github.com/tkuester/taky
clone 10-tak https://github.com/snstac/adsbxcot
clone 10-tak https://github.com/snstac/stratuxcot
clone 10-tak https://github.com/snstac/cotproxy
clone 10-tak https://github.com/kylesayrs/ATAK_push_cots
clone 10-tak https://github.com/deptofdefense/AndroidTacticalAssaultKit-CIV

# Area 11: 3D GIS
clone 11-3d-gis https://github.com/CesiumGS/cesium
clone 11-3d-gis https://github.com/maplibre/maplibre-gl-js
clone 11-3d-gis https://github.com/maptalks/maptalks.js
clone 11-3d-gis https://github.com/visgl/deck.gl
clone 11-3d-gis https://github.com/keplergl/kepler.gl
clone 11-3d-gis https://github.com/TerriaJS/terriajs
clone 11-3d-gis https://github.com/openlayers/openlayers
clone 11-3d-gis https://github.com/openmaptiles/openmaptiles
clone 11-3d-gis https://github.com/davenquinn/cesium-vector-provider
clone 11-3d-gis https://github.com/OSGeo/gdal

# Area 12: SDR/RF
clone 12-sdr-rf https://github.com/gnuradio/gnuradio
clone 12-sdr-rf https://github.com/opendroneid/opendroneid-core-c
clone 12-sdr-rf https://github.com/proto17/dji_droneid
clone 12-sdr-rf https://github.com/opendroneid/receiver-android
clone 12-sdr-rf https://github.com/tesorrells/RF-Drone-Detection
clone 12-sdr-rf https://github.com/AlexandreRouma/SDRPlusPlus
clone 12-sdr-rf https://github.com/gqrx-sdr/gqrx
clone 12-sdr-rf https://github.com/gnss-sdr/gnss-sdr
clone 12-sdr-rf https://github.com/martinmarinov/TempestSDR
clone 12-sdr-rf https://github.com/srsran/srsRAN_Project

echo ""
echo "─── Tamamlandı ───"
du -sh "$ROOT"
