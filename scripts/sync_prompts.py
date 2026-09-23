"""Seed Langfuse Prompt Management from the instruction texts in code.

Makes no model call — it costs nothing but a few requests to Langfuse.

    python scripts/sync_prompts.py            # create what is missing, touch nothing else
    python scripts/sync_prompts.py --update   # also push the code text where production differs

Langfuse is where the prompts are edited, so the default never overwrites.
--update is for when the code text is the one that moved on: it creates a NEW
version labelled production; the one it replaces stays in Langfuse and can be
promoted back from the UI.
"""

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langfuse.api import NotFoundError  # noqa: E402

from app import observability  # noqa: E402
from app.prompt_registry import PROMPTS  # noqa: E402
from app.prompts import LABEL  # noqa: E402


def sync(client: Any, registry: dict[str, str], *, update: bool) -> list[str]:
    report: list[str] = []
    for name, code_text in registry.items():
        try:
            current = client.get_prompt(name, label=LABEL, type="text", cache_ttl_seconds=0, max_retries=0)
        except NotFoundError:
            client.create_prompt(
                name=name, prompt=code_text, labels=[LABEL], type="text", commit_message="Seeded from code"
            )
            report.append(f"{name}: created")
            continue
        if current.prompt == code_text:
            report.append(f"{name}: exists v{current.version}")
        elif update:
            client.create_prompt(
                name=name, prompt=code_text, labels=[LABEL], type="text", commit_message="Synced from code"
            )
            report.append(f"{name}: updated (was v{current.version})")
        else:
            report.append(f"{name}: exists v{current.version} (differs from code)")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed Langfuse Prompt Management from the texts in code.")
    parser.add_argument(
        "--update", action="store_true", help="push the code text as a new production version where it differs"
    )
    args = parser.parse_args(argv)
    client = observability.client()
    if client is None:
        print("Langfuse is not configured: set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY.", file=sys.stderr)
        return 1
    for line in sync(client, PROMPTS, update=args.update):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
