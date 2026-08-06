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
    "historical_media.py",
]
for name in required_source:
    path = SRC / name
    assert path.is_file(), name
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

for name in ("build_public_release.py", "public_launcher.pyw", "uninstall.pyw"):
    path = ROOT / "packaging" / name
    assert path.is_file(), name
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

for name in ("install_public.ps1", "launcher_guard.ps1", "run_visible.ps1", "copy_recent_logs.ps1"):
    assert (ROOT / "packaging" / name).is_file(), name

for name in (
    "Install.bat", "Start.bat", "Update and Start.bat",
    "Install-from-source.ps1", "Update-source.ps1", "SOURCE_INSTALL.md",
):
    assert (ROOT / name).is_file(), name
assert (SRC / "danbooru_artist_tags_v4.5.txt").is_file()

source_installer = (ROOT / "Install-from-source.ps1").read_text(encoding="utf-8")
source_updater = (ROOT / "Update-source.ps1").read_text(encoding="utf-8")
assert 'ARTIST_RANKER_SOURCE_INSTALL = "1"' in source_installer
assert "ARTIST_RANKER_DATA_DIR" in source_installer
assert 'Join-Path $RepoRoot ".venv"' in source_installer
assert "requirements.sha256" in source_installer
assert '"release"' in source_updater
assert "status --porcelain" in source_updater
assert '"pull", "--ff-only"' in source_updater
assert "reset --hard" not in source_updater.casefold()

launcher_source = (ROOT / "packaging" / "public_launcher.pyw").read_text(encoding="utf-8")
install_source = (ROOT / "packaging" / "install_public.ps1").read_text(encoding="utf-8")
visible_source = (ROOT / "packaging" / "run_visible.ps1").read_text(encoding="utf-8")
assert 'APP_VERSION = "2.6.1"' in launcher_source
assert "user_paths.json" in launcher_source + install_source + visible_source
assert 'ForEach-Object { $_.ToString() }' in visible_source
assert '$ErrorActionPreference = "Continue"' in visible_source

for name in (
    "LICENSE", "SECURITY.md", "BUILDING.md", "FOLDER_LAYOUT.md", "THIRD_PARTY_NOTICES.md",
    "SECURITY_DEPENDENCIES.md", "requirements.lock.txt", "DEPENDENCY_INVENTORY.json",
):
    assert (ROOT / name).is_file(), name

main_source = (SRC / "artist_elo_ranker_buffered.py").read_text(encoding="utf-8")
recovery_source = (SRC / "backup_transfer_recovery.py").read_text(encoding="utf-8")
guidance_source = (SRC / "onboarding_guidance.py").read_text(encoding="utf-8")
builder_source = (ROOT / "packaging" / "build_public_release.py").read_text(encoding="utf-8")
assert 'SHAREABLE_EDITION_VERSION = "2.6.1"' in main_source
assert "SOURCE_INSTALL_MODE" in main_source
assert "Update and Start.bat" in main_source
assert 'GITHUB_REPOSITORY = "SeaBassBand/NovelAI-Artist-Ranker"' in main_source
assert 'DEFAULT_GITHUB_REPOSITORY = "SeaBassBand/NovelAI-Artist-Ranker"' in recovery_source
assert "example.invalid" not in main_source + recovery_source
assert "const CACHE='artist-elo-duel-v8'" in main_source
assert "!SHELL_PATHS.has(url.pathname)" in main_source
assert "ssr_mode=False" in main_source
assert "__artistEloGalleryArtistPickerLifecycle" in main_source
assert '"storage_settings": 4' in guidance_source
assert '"dedicated_duel": 2' in guidance_source
assert '"android_app": 4' in guidance_source
assert "PYTHONDONTWRITEBYTECODE" in builder_source
assert '"-B", "-I", "-c"' in builder_source
ranker_start = main_source.index("ranker = ArtistELORanker()")
backup_log = main_source.index('print(f"Backup compartment: {ranker.transfer_recovery.backup_root}")')
assert ranker_start < backup_log, "startup logging must not access ranker before it is created"

android_manifest = ROOT / "android-builder" / "project" / "app" / "src" / "main" / "AndroidManifest.xml"
android_activity = ROOT / "android-builder" / "project" / "app" / "src" / "main" / "java" / "com" / "sebas" / "artistranker" / "MainActivity.java"
android_monitor = android_activity.with_name("BufferMonitorSupport.java")
assert android_manifest.is_file()
assert android_activity.is_file()
assert 'android:scheme="artist-ranker"' in android_manifest.read_text(encoding="utf-8")
assert 'android:allowBackup="false"' in android_manifest.read_text(encoding="utf-8")
assert "handleDeepLinkIntent" in android_activity.read_text(encoding="utf-8")
monitor_source = android_monitor.read_text(encoding="utf-8")
assert "CookieManager.getInstance().getCookie(base)" in monitor_source
assert "http://artist-ranker.local:7860" in monitor_source
assert "LapSebas" not in monitor_source

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
