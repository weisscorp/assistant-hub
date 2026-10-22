FROM python:3.13-slim-bookworm AS runtime
LABEL org.opencontainers.image.source="https://github.com/weisscorp/assistant-hub"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/data
RUN groupadd --gid 10001 hub && useradd --uid 10001 --gid 10001 --no-create-home hub \
    && mkdir /data && chown hub:hub /data
WORKDIR /opt/assistant-hub/backend
USER 10001:10001

FROM runtime AS collectors
COPY backend/hub_collectors.py backend/hub_collector_plugins.py ./
ENV PYTHONPATH=/plugins:/opt/assistant-hub/backend
ENTRYPOINT ["python3", "hub_collectors.py"]
CMD ["run", "--config", "/config/collectors.toml"]

FROM runtime AS hub
COPY backend/*.py ./
COPY backend/hub_ui/ ./hub_ui/
COPY tools/*.py ../tools/
COPY LICENSE THIRD_PARTY.md ../
EXPOSE 8778
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python3", "hub_container.py", "healthcheck"]
ENTRYPOINT ["python3", "hub_container.py"]
CMD ["serve"]
