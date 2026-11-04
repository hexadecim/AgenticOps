# Build context: repository root (uv workspace). docker build -f services/mail-service/Dockerfile .
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY libs ./libs
COPY services ./services
RUN uv sync --frozen --no-dev --package mail-service
ENV PATH="/app/.venv/bin:$PATH"
RUN useradd --system --uid 10001 opspilot
USER opspilot
WORKDIR /app/services/mail-service
EXPOSE 8003
CMD ["sh", "-c", "if [ \"$RUN_MIGRATIONS\" != false ]; then alembic upgrade head; fi && exec uvicorn --factory mail_service.main:create_app --host 0.0.0.0 --port 8003 --proxy-headers"]
