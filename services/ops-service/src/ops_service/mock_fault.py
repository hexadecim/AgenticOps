"""Re-open a fault in the mock environment, for demos:
`python -m ops_service.mock_fault upload-worker eu-west-1`.

Only the simulation changes; the next probes report the service as failing until an approved fix runs."""

from __future__ import annotations

import argparse
import asyncio

from .bootstrap import build_container
from .config import Settings


async def main(service: str, region: str, detail: str) -> None:
    container = build_container(Settings())
    try:
        await container.world.inject(service, region, detail)
        print(f"Fault open: {service} in {region} ({detail})")
    finally:
        for close in container.closers:
            await close()
        await container.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service")
    parser.add_argument("region")
    parser.add_argument("--detail", default="storage write failed: EIO on /vol/uploads")
    args = parser.parse_args()
    asyncio.run(main(args.service, args.region, args.detail))
