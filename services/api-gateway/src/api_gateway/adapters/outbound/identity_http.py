"""identity-service client. Every call goes through the `identity-service` circuit breaker."""

from __future__ import annotations

from ...ports import Json
from .service_http import ServiceHttpClient


class IdentityHttpClient(ServiceHttpClient):
    async def sign_in(self, email: str, password: str) -> Json:
        return await self._obj("POST", "/v1/sign-in", json={"email": email, "password": password})

    async def sign_in_mfa(self, challenge_token: str, code: str, new_password: str | None) -> Json:
        return await self._obj(
            "POST",
            "/v1/sign-in/mfa",
            json={"challenge_token": challenge_token, "code": code, "new_password": new_password},
        )

    async def renew(self, token: str) -> Json:
        return await self._obj("POST", "/v1/session/renew", token=token)

    async def sign_out(self, token: str, reason: str) -> None:
        await self._call("POST", "/v1/sign-out", token=token, params={"reason": reason})

    async def me(self, token: str) -> Json:
        return await self._obj("GET", "/v1/me", token=token)

    async def roles(self, token: str) -> list[Json]:
        return await self._list("GET", "/v1/roles", token=token)

    async def list_users(self, token: str) -> list[Json]:
        return await self._list("GET", "/v1/users", token=token)

    async def create_user(self, token: str, body: Json) -> Json:
        return await self._obj("POST", "/v1/users", token=token, json=body)

    async def update_user(self, token: str, user_id: str, body: Json) -> Json:
        return await self._obj("PATCH", f"/v1/users/{user_id}", token=token, json=body)

    async def reset_mfa(self, token: str, user_id: str) -> Json:
        return await self._obj("POST", f"/v1/users/{user_id}/reset-mfa", token=token)

    async def reset_password(self, token: str, user_id: str, body: Json) -> Json:
        return await self._obj("POST", f"/v1/users/{user_id}/reset-password", token=token, json=body)
