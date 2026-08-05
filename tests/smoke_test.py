from __future__ import annotations
import ast
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])(?:pst|sk)-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")

for benign in (
    "asterisk-linking-protocols-exception",
    "risk-assessment-license-text",
    "task-runner-package-metadata",
):
    assert not TOKEN_RE.search(benign), benign
for credential in ("sk-" + "A" * 32, "pst-" + "B" * 32):
    assert TOKEN_RE.search(credential), credential

required_source = [
    "artist_elo_ranker_buffered.py", "ranker_data_layout.py", "novelai_credential_store.py",
    "generation_profiles.py", "storage_retention.py", "phone_pairing.py",
    "onboarding_guidance.py", "backup_transfer_recovery.py", "lan_hostname.py",
]
for name in required_source:
    path = SRC / name
    assert path.is_file(), name
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

for name in ("build_public_release.py", "public_launcher.pyw", "uninstall.pyw"):
    path = ROOT / "packaging" / name
    assert path.is_file(), name
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

for name in ("LICENSE", "SECURITY.md", "BUILDING.md", "FOLDER_LAYOUT.md", "THIRD_PARTY_NOTICES.md", "requirements.lock.txt"):
    assert (ROOT / name).is_file(), name

for path in ROOT.rglob("*"):
    if not path.is_file():
        continue
    lower_parts = {part.casefold() for part in path.relative_to(ROOT).parts}
    assert not lower_parts.intersection({"signing", "toolchain", "gradle-cache", ".gradle", "diagnostics", "comparison_images"}), path
    assert path.suffix.casefold() not in {".jks", ".keystore"}, path
    if path.suffix.lower() in {".py", ".pyw", ".md", ".txt", ".json", ".yml", ".yaml", ".ps1", ".bat"}:
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not TOKEN_RE.search(text), path
print("PUBLIC_REPOSITORY_SMOKE_OK")
