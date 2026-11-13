FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY config ./config
ENV DP_DB_PATH=/data/decisions.db DP_CONFIG_DIR=/app/config/workflows
VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "decision_platform.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
