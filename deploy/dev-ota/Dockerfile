# Build from the repository root. The release identity/signing key never enters this image.
FROM --platform=linux/amd64 python:3.13-slim-bookworm
LABEL org.opencontainers.image.source="https://github.com/weisscorp/assistant-hub"

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl unzip openjdk-17-jre-headless \
    && curl --fail --location --silent --show-error \
      https://dl.google.com/android/repository/build-tools_r35_linux.zip -o /tmp/build-tools.zip \
    && test "$(stat -c '%s' /tmp/build-tools.zip)" = '61958799' \
    && echo 'bd3a4966912eb8b30ed0d00b0cda6b6543b949d5ffe00bea54c04c81e1561d88  /tmp/build-tools.zip' | sha256sum -c - \
    && unzip -q /tmp/build-tools.zip -d /opt \
    && mv /opt/android-15 /opt/android-build-tools \
    && rm /tmp/build-tools.zip \
    && apt-get purge -y --auto-remove curl unzip \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 ota && useradd --uid 10001 --gid 10001 --no-create-home ota

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    OTA_PORT=8443 OTA_OUTPUT=/data/releases OTA_SECRET_DIR=/var/run/ota-secrets \
    OTA_PRIVATE_DIR=/run/ota/identity OTA_BUILD_TOOLS=/opt/android-build-tools
WORKDIR /opt/assistant-dev-ota
COPY backend/dev_ota.py backend/dev_ota_container.py backend/build_release.py backend/make_release.py ./
USER 10001:10001
EXPOSE 8443
CMD ["python3", "dev_ota_container.py"]
