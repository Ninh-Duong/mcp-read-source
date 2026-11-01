"""Time the ai-context pipeline against a local repository.

Usage: uv run --locked python scripts/benchmark.py <repository-root> [--query TEXT]

The index is written to a temporary directory that is deleted afterwards. Only timings and counts
are printed, never the repository path.
"""
from __future__ import annotations

import argparse
import statistics
import tempfile
import time
from pathlib import Path

from mcp_read_source import crypto
from mcp_read_source.server import SourceReader


def timed(label: str, action, repeat: int = 1):
    samples = []
    result = None
    for _ in range(repeat):
        started = time.perf_counter()
        result = action()
        samples.append((time.perf_counter() - started) * 1000)
    suffix = f" (median of {repeat})" if repeat > 1 else ""
    print(f"{label:<34} {statistics.median(samples):9.1f} ms{suffix}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--query", default="get", help="find_entry query used to pick an entry to trace")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as context_dir:
        reader = SourceReader({"bench": args.root}, context_dir=context_dir)
        try:
            first = timed("first build", lambda: reader.index("bench"))
            timed("gate, commit unchanged", lambda: reader.index("bench"), 20)
            other = SourceReader({"bench": args.root}, context_dir=context_dir)
            timed("gate in a new process (decrypt)", lambda: other.index("bench"))
            other.store.close()
            timed("forced incremental rescan", lambda: reader.index("bench", force=True))
            store = reader.store
            path, loaded = next(iter(store._loaded.items()))
            label = crypto.label_hash("bench", first["branch"])
            timed("seal + atomic write (save)", lambda: store._save(path, label, loaded.connection), 5)
            found = timed("find_entry", lambda: reader.find_entry("bench", args.query, limit=1), 5)
            if found["matches"]:
                entry = found["matches"][0]["id"]
                timed("trace", lambda: reader.trace("bench", entry), 5)
                timed("read_symbol", lambda: reader.read_symbol("bench", entry), 5)
            timed("search (symbol)", lambda: reader.search("bench", args.query, mode="symbol"), 5)
            timed("search (text fallback)", lambda: reader.search("bench", args.query, mode="text"), 3)
            database = next(Path(context_dir).glob("*.sqlite.enc"))
            print(f"indexed: {first['indexed']} entries: {first['entries']}")
            print(f"encrypted database size: {database.stat().st_size / 1e6:.1f} MB")
        finally:
            reader.store.close()


if __name__ == "__main__":
    main()
