from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from typing import List, Set


SOURCE = next(
    candidate
    for candidate in (
        Path(__file__).with_name("artist_elo_ranker_buffered.py"),
        Path(__file__).resolve().parents[1] / "src" / "artist_elo_ranker_buffered.py",
        Path(__file__).resolve().parents[1] / "artist_elo_ranker_buffered.py",
        Path(__file__).resolve().parents[2] / "artist_elo_ranker_buffered.py",
    )
    if candidate.is_file()
)
source_text = SOURCE.read_text(encoding="utf-8")
tree = ast.parse(source_text, filename=str(SOURCE))

method_node = None
for node in tree.body:
    if isinstance(node, ast.ClassDef) and node.name == "ArtistELORanker":
        method_node = next(
            child
            for child in node.body
            if isinstance(child, ast.FunctionDef) and child.name == "handle_portrait_request"
        )
        break
assert method_node is not None


class Resolver:
    def resolve(self, stored_path, **_kwargs):
        return SimpleNamespace(
            available=bool(stored_path),
            resolved_path=str(stored_path),
        )


namespace = {
    "json": json,
    "List": List,
    "Set": Set,
    "Path": Path,
    "os": os,
    "time": time,
    "HISTORICAL_MEDIA_RESOLVER": Resolver(),
    "PORTRAIT_RECENT_DUEL_LIMIT": 20,
}
exec(compile(ast.Module(body=[method_node], type_ignores=[]), str(SOURCE), "exec"), namespace)


class DummyRanker:
    handle_portrait_request = namespace["handle_portrait_request"]

    def __init__(self):
        self.artist_manager = SimpleNamespace(artists=["test_artist"])
        self.artist_portraits = SimpleNamespace(get=lambda _artist: None)
        records = []
        for number in range(1, 26):
            team = ["test_artist"] if number % 3 == 1 else ["test_artist", f"partner_{number}"]
            if number % 3 == 0:
                team.append(f"third_{number}")
            records.append({
                "artists_a": team,
                "artists_b": [f"opponent_{number}"],
                "image_a_path": f"image_{number}.webp",
                "image_b_path": f"other_{number}.webp",
                "winner": "A",
                "timestamp": float(number),
            })
        self.history = SimpleNamespace(records=records)

    @staticmethod
    def _portrait_preview_data_url(path):
        return f"preview:{path}"


result = json.loads(DummyRanker().handle_portrait_request(json.dumps({"artist": "test_artist"})))
assert result["ok"] is True
assert result["duel_limit"] == 20
assert len(result["candidates"]) == 20
assert [item["history_number"] for item in result["candidates"]] == list(range(25, 5, -1))
assert {item["team_size"] for item in result["candidates"]} == {1, 2, 3}

assert '"portrait_url": portrait_url' in source_text
assert '@server.get("/api/duel/artist/portrait")' in source_text
assert "DEDICATED_ARTIST_PORTRAITS_V241" in source_text
assert "ladder-portrait" in source_text
print("PORTRAIT_FEATURE_TEST_OK")
