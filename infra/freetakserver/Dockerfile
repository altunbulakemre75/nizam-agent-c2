# FreeTAKServer — self-build (public registry image bulunamadı)
# Usage: docker compose --profile tak build freetakserver
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git gcc \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    FreeTAKServer \
    FreeTAKServer-UI

WORKDIR /opt/FTSData
EXPOSE 8087 8089 8443 19023

# FTS default — CoT TCP plaintext, HTTPS UI, API
CMD ["python", "-m", "FreeTAKServer.controllers.services.FTS"]
