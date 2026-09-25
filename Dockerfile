FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CCM_ENV=production \
    CCM_AUTH_MODE=session \
    CCM_DATABASE_URL=sqlite:////data/credit-card-manager.db \
    CCM_ERROR_LOG_PATH=/data/logs/errors.log

WORKDIR /app

RUN set -eux; \
    ARCH="$(dpkg --print-architecture)"; \
    case "$ARCH" in \
        amd64|arm64) ;; \
        *) echo "unsupported architecture: $ARCH" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    curl -fsSL "https://downloads.rclone.org/v1.75.1/rclone-v1.75.1-linux-${ARCH}.deb" -o /tmp/rclone.deb; \
    dpkg -i /tmp/rclone.deb; \
    rm /tmp/rclone.deb; \
    apt-get purge -y --auto-remove curl; \
    rm -rf /var/lib/apt/lists/*; \
    rclone version

COPY server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY server ./server
COPY web ./web
RUN python /app/web/apply_card_management_ui.py /app/web/index.html \
    && python /app/web/apply_card_settings_ui.py /app/web/index.html \
    && rm /app/web/apply_card_management_ui.py /app/web/apply_card_settings_ui.py
WORKDIR /app/server
RUN chmod +x scripts/docker-entrypoint.sh

EXPOSE 8000

CMD ["./scripts/docker-entrypoint.sh"]
