"""Generate a repeatable synthetic call log."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from random import Random


def write_sample(path: Path, seed: int) -> None:
    rng = Random(seed)
    start = datetime.fromisoformat("2026-11-27T10:00:00+08:00")
    with path.open("w", encoding="utf-8", newline="\n") as log:
        for index in range(200):
            user = rng.choice(("alice", "bob", "carol"))
            model = rng.choice(("model-a", "model-b"))
            status = rng.choices(("ok", "timeout", "error"), weights=(93, 3, 4))[0]
            tokens_in = rng.randint(8, 16)
            tokens_out = rng.randint(6, 20)
            if index < 3:
                user, model = "alice", "model-a"
                status = "timeout" if index < 2 else "ok"
            if index == 2:
                tokens_out = 5001
            record = {
                "ts": (start + timedelta(seconds=45 * index)).isoformat(),
                "request_id": f"r{index + 1:04d}",
                "user": user,
                "model": model,
                "latency_ms": rng.randint(140, 1800),
                "status": status,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
            }
            print(json.dumps(record, separators=(",", ":")), file=log)
            if index == 24:
                print("not json", file=log)
        print('{"request_id":"broken"}', file=log)
