#!/bin/sh
# Real identity-service and gateway for the sign-in test, on the test infrastructure (make test-infra-up): port 55432,
# never the dev database. Every run starts with an empty user table, so the first admin enrols again.
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export JWT_SECRET="e2e-only-secret-at-least-32-characters-long"
export FERNET_KEY="$(uv run --project "$ROOT" python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export REDIS_URL="redis://localhost:56379/1"
export RABBITMQ_URL="amqp://opspilot:opspilot@localhost:55672/"
export DATABASE_URL="postgresql+asyncpg://identity_svc:identity_dev_password@localhost:55432/opspilot"
export BOOTSTRAP_ADMIN_EMAIL="e2e-admin@opspilot.local"
export BOOTSTRAP_ADMIN_PASSWORD="a-long-synthetic-e2e-passphrase"
export LOG_JSON=false

cd "$ROOT/services/identity-service"
uv run alembic upgrade head
uv run python - <<'PY'
import asyncio, os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
async def main():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE identity.users CASCADE"))
    await engine.dispose()
asyncio.run(main())
PY
uv run uvicorn --factory identity_service.main:create_app --port 18001 --log-level warning &
IDENTITY=$!
cd "$ROOT/services/api-gateway"
IDENTITY_URL=http://localhost:18001 LIVE_EVENTS=false uv run uvicorn --factory api_gateway.main:create_app --port 18000 --log-level warning &
GATEWAY=$!
trap 'kill $IDENTITY $GATEWAY 2>/dev/null' EXIT INT TERM
cd "$ROOT/frontend"
npm run build >/dev/null
GATEWAY_URL=http://localhost:18000 npx vite preview --port 4173 --strictPort
