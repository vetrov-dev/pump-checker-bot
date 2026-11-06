"""Fill the database from old event recordings (data/events_*.jsonl written by sol_scalper), so dev history
starts before the bot itself was running.

    python seed.py ../sol_scalper/data
"""
import asyncio
import json
import sys
import time
from pathlib import Path

from config import load
from store import Store
from tracker import Tracker


async def main(folder: Path) -> None:
    store = Store(load().db_path)
    tracker = Tracker(store)          # no notifications while seeding
    files = sorted(folder.glob("events_*.jsonl"))
    if not files:
        sys.exit(f"No events_*.jsonl in {folder}")
    started, n = time.time(), 0
    for f in files:
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if "t" not in ev or not ev.get("mint"):
                    continue
                await tracker.on_event(ev)
                n += 1
                if n % 200_000 == 0:
                    tracker.flush()
                    print(f"{n:,} events, {tracker.counts['create']:,} launches", flush=True)
        print(f"done {f.name}")
    tracker.flush()
    print(f"Imported {n:,} events in {time.time() - started:.0f} s: {tracker.counts['create']:,} launches, "
          f"{tracker.counts['migrate']:,} graduations")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1] if len(sys.argv) > 1 else "../sol_scalper/data")))
