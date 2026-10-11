"""Print Backboard LLM models whose name contains 'gemma'."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backboard import BackboardClient

from app.config import settings


async def list_gemma_models() -> list[tuple[str, str, int]]:
    if not settings.backboard_api_key:
        raise SystemExit("BACKBOARD_API_KEY is missing")

    hits: list[tuple[str, str, int]] = []
    async with BackboardClient(api_key=settings.backboard_api_key, timeout=60) as client:
        skip = 0
        limit = 500
        total = None
        while True:
            page = await client.list_models(model_type="llm", skip=skip, limit=limit)
            if total is None:
                total = page.total
            for model in page.models:
                blob = f"{model.name} {model.display_name or ''}".lower()
                if "gemma" in blob:
                    hits.append((model.provider, model.name, model.context_limit))
            skip += limit
            if skip >= page.total or not page.models:
                break
    hits.sort()
    return hits


async def main() -> None:
    hits = await list_gemma_models()
    if not hits:
        print("No models with 'gemma' in the name.")
        return
    print(f"Found {len(hits)} Gemma model(s):")
    for provider, name, context in hits:
        print(f"  provider={provider}  model={name}  context={context}")


if __name__ == "__main__":
    asyncio.run(main())
