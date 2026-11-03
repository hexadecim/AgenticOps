from __future__ import annotations

from typing import Any

from opspilot_common.resilience import ResilienceError, ResilientHttpClient

from ....ports import Check, ConnectionReport
from ._common import explain


class OllamaTester:
    def __init__(self, http: ResilientHttpClient) -> None:
        self._http = http

    async def test(self, values: dict[str, Any], secrets: dict[str, str]) -> ConnectionReport:
        url = values["ollama_url"]
        try:
            version = (await self._http.get(f"{url}/api/version")).json().get("version", "unknown")
            tags = (await self._http.get(f"{url}/api/tags")).json()
        except ResilienceError as exc:
            return ConnectionReport.from_checks(
                [Check("Reach Ollama", False, explain(exc, f"Ollama at {url}"))], ""
            )
        except ValueError:
            return ConnectionReport.from_checks(
                [Check("Reach Ollama", False, f"{url} is not an Ollama server.")], ""
            )
        installed = {m["name"] for m in tags.get("models", [])} | {
            m["name"].split(":")[0] for m in tags.get("models", []) if m["name"].endswith(":latest")
        }
        checks = [Check("Reach Ollama", True, f"Ollama {version} at {url}")]
        for key, purpose in (("model", "Chat model"), ("embedding_model", "Embedding model")):
            name = values[key]
            ok = name in installed
            checks.append(
                Check(
                    purpose,
                    ok,
                    f"{name} is installed" if ok else f"{name} is not installed. Run: ollama pull {name}",
                )
            )
        return ConnectionReport.from_checks(checks, f"Ollama is ready with {values['model']}.")
