# Build context: repository root (uv workspace). docker build -f services/agent-orchestrator/Dockerfile .
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY libs ./libs
COPY services ./services
RUN uv sync --frozen --no-dev --package agent-orchestrator
ENV PATH="/app/.venv/bin:$PATH"
RUN useradd --system --uid 10001 opspilot
USER opspilot
WORKDIR /app/services/agent-orchestrator
EXPOSE 8008
CMD ["sh", "-c", "alembic upgrade head && uvicorn --factory agent_orchestrator.main:create_app --host 0.0.0.0 --port 8008 --proxy-headers"]
