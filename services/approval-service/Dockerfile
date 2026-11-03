# Build context: repository root (uv workspace). docker build -f services/approval-service/Dockerfile .
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY libs ./libs
COPY services ./services
RUN uv sync --frozen --no-dev --package approval-service
ENV PATH="/app/.venv/bin:$PATH"
RUN useradd --system --uid 10001 opspilot
USER opspilot
WORKDIR /app/services/approval-service
EXPOSE 8006
CMD ["sh", "-c", "alembic upgrade head && uvicorn --factory approval_service.main:create_app --host 0.0.0.0 --port 8006 --proxy-headers"]
