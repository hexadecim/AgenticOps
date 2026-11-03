"""Development seed: one demo user per role, each with a temporary password (changed at first sign-in).

uv run --package identity-service python -m identity_service.seed
"""

from __future__ import annotations

import asyncio
import secrets

from opspilot_common.auth import Principal, Role

from .bootstrap import build_container
from .config import Settings
from .domain.errors import EmailAlreadyRegisteredError

DEMO_USERS = [
    ("analyst@opspilot.local", "Lena Analyst", Role.L1_ANALYST),
    ("agent@opspilot.local", "Sam Agent", Role.SUPPORT_AGENT),
    ("manager@opspilot.local", "Maria Manager", Role.SUPPORT_MANAGER),
    ("compliance@opspilot.local", "Chris Compliance", Role.COMPLIANCE_LEAD),
]


async def main() -> None:
    container = build_container(Settings())
    seeder = Principal(user_id="seed-script", name="Seed script", roles=frozenset({Role.ADMIN}))
    print(f"{'email':<28} {'role':<18} temporary password")
    for email, name, role in DEMO_USERS:
        password = f"Temp-{secrets.token_urlsafe(9)}"
        try:
            await container.create_user(
                seeder, email=email, name=name, roles=frozenset({role}), temporary_password=password
            )
            print(f"{email:<28} {role.value:<18} {password}")
        except EmailAlreadyRegisteredError:
            print(f"{email:<28} {role.value:<18} (already exists)")
    await container.engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
