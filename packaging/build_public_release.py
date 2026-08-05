#!/usr/bin/env python3
"""Build the Phase 8 self-contained Windows release from an installed developer tree."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

APP_VERSION = "2.5.3"
ANDROID_VERSION = "1.4.1"
ANDROID_CODE = 10
RELEASE_FOLDER = f"NovelAI-Artist-Ranker-v{APP_VERSION}"
TEXT_SUFFIXES = {".py", ".pyw", ".txt", ".md", ".json", ".yml", ".yaml", ".xml", ".gradle", ".bat", ".cmd", ".ps1"}
TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])(?:pst|sk)-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")
PRIVATE_NAMES = {
    ".env", "keystore.properties", "local.properties", "artist-ranker-release.jks",
    "phone_pairing.json", "novelai_api_key", "credential",
}
PROGRAM_ALLOWLIST = (
    "artist_elo_ranker_buffered.py", "ranker_data_layout.py", "novelai_credential_store.py",
    "generation_profiles.py", "storage_retention.py", "phone_pairing.py",
    "onboarding_guidance.py", "backup_transfer_recovery.py", "lan_hostname.py", "qrcode",
)
RUNTIME_FILES = ("public_launcher.pyw", "uninstall.pyw", "config.py")

def source_fallback_root(payload_root: Path) -> Path:
    if (payload_root / "artist_elo_ranker_buffered.py").is_file():
        return payload_root
    repository_source = payload_root.parent / "src"
    if (repository_source / "artist_elo_ranker_buffered.py").is_file():
        return repository_source
    return payload_root

def runtime_assets_root(payload_root: Path) -> Path:
    standard = payload_root / "public_runtime"
    return standard if standard.is_dir() else payload_root

def repository_template_root(payload_root: Path) -> tuple[Path, bool]:
    standard = payload_root / "public_repo_template"
    if standard.is_dir():
        return standard, False
    repository = payload_root.parent
    if (repository / "src").is_dir() and (repository / "packaging").is_dir():
        return repository, True
    raise FileNotFoundError("Public repository template was not found.")

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def copytree_filtered(source: Path, destination: Path, ignore_names: set[str] | None = None, *, skip_tests: bool = True) -> None:
    ignore_names = ignore_names or set()
    if destination.exists():
        shutil.rmtree(destination)
    def ignore(_root, names):
        blocked = {"__pycache__"}
        if skip_tests:
            blocked.update({"test", "tests"})
        return [name for name in names if name in ignore_names or name in blocked]
    shutil.copytree(source, destination, ignore=ignore)

def validate_token_scanner() -> None:
    # SPDX and dependency metadata contain ordinary identifiers such as
    # ``asterisk-linking-protocols-exception``.  The old unbounded ``sk-``
    # pattern matched the middle of that word.  Keep this regression check next
    # to the scanner so future pattern edits cannot reintroduce the false positive.
    benign = (
        "asterisk-linking-protocols-exception",
        "risk-assessment-license-text",
        "task-runner-package-metadata",
    )
    for value in benign:
        if TOKEN_RE.search(value):
            raise RuntimeError(f"Credential scanner rejected benign dependency metadata: {value}")
    for value in ("sk-" + "A" * 32, "pst-" + "B" * 32):
        if not TOKEN_RE.search(value):
            raise RuntimeError("Credential scanner no longer detects a supported token format.")

def scan_private(root: Path) -> None:
    validate_token_scanner()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        lowered = path.name.casefold()
        if lowered in PRIVATE_NAMES or path.suffix.casefold() in {".jks", ".keystore"}:
            raise RuntimeError(f"Private material entered the public release: {path}")
        if path.suffix.casefold() in TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8", errors="replace")
            if TOKEN_RE.search(text):
                raise RuntimeError(f"API-token literal entered the public release: {path}")

def write_zip(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(p for p in source.rglob("*") if p.is_file()):
            archive.write(path, path.relative_to(source).as_posix())
    with zipfile.ZipFile(destination) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"ZIP CRC verification failed at {bad}")

def locate_artist_list(project: Path) -> Path:
    candidates = [
        project / "danbooru_artist_tags_v4.5.txt",
        project / "resources" / "danbooru_artist_tags_v4.5.txt",
        project / "danbooru_artist_tags.txt",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("The public build requires danbooru_artist_tags_v4.5.txt in the project or resources folder.")

def freeze_requirements(python_exe: Path) -> str:
    # Record exact installed distribution versions without leaking local checkout paths
    # that can appear in `pip freeze` for editable or direct-file installations.
    code = (
        "import importlib.metadata as m,json;"
        "rows=[];"
        "[(rows.append({'name':d.metadata.get('Name'),'version':d.version})) "
        "for d in m.distributions() if d.metadata.get('Name')];"
        "print(json.dumps(rows))"
    )
    result = subprocess.run([str(python_exe), "-I", "-c", code], check=True, capture_output=True, text=True)
    rows = json.loads(result.stdout)
    versions: dict[str, tuple[str, str]] = {}
    for row in rows:
        name = str(row.get("name", "")).strip()
        version = str(row.get("version", "")).strip()
        if not name or not version:
            continue
        key = re.sub(r"[-_.]+", "-", name).casefold()
        previous = versions.get(key)
        if previous and previous[1] != version:
            raise RuntimeError(f"Conflicting installed versions for {name}: {previous[1]} and {version}")
        versions[key] = (name, version)
    if not versions:
        raise RuntimeError("Could not enumerate the project environment's installed dependencies.")
    return "\n".join(f"{versions[key][0]}=={versions[key][1]}" for key in sorted(versions)) + "\n"

def copy_python_runtime(project: Path, stage: Path, fixture_runtime: Path | None = None) -> tuple[Path, str]:
    runtime = stage / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    if fixture_runtime:
        copytree_filtered(fixture_runtime, runtime)
        python_exe = runtime / "python.exe"
        pythonw_exe = runtime / "pythonw.exe"
        if not python_exe.exists():
            python_exe.write_bytes(b"fixture")
        if not pythonw_exe.exists():
            pythonw_exe.write_bytes(b"fixture")
        return python_exe, "fixture-runtime==1\n"
    if os.name != "nt":
        raise RuntimeError("A real public Windows runtime must be built on Windows. Use --fixture-runtime only for tests.")
    venv = project / "venv"
    venv_python = venv / "Scripts" / "python.exe"
    if not venv_python.is_file():
        raise FileNotFoundError("The active project venv was not found.")
    probe = subprocess.run(
        [str(venv_python), "-c", "import json,sys;print(json.dumps({'base':sys.base_prefix,'prefix':sys.prefix,'version':list(sys.version_info[:3])}))"],
        check=True, capture_output=True, text=True)
    info = json.loads(probe.stdout.strip())
    base = Path(info["base"])
    if int(info["version"][0]) != 3 or int(info["version"][1]) != 11:
        raise RuntimeError("The public runtime builder currently requires the project's supported Python 3.11 environment.")
    for name in ("python.exe", "pythonw.exe", "python3.dll", "python311.dll", "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt"):
        source = base / name
        if source.exists():
            shutil.copy2(source, runtime / name)
    copytree_filtered(base / "DLLs", runtime / "DLLs")
    shutil.copytree(base / "Lib", runtime / "Lib", ignore=shutil.ignore_patterns("site-packages", "__pycache__", "test", "tests"))
    if (base / "tcl").is_dir():
        copytree_filtered(base / "tcl", runtime / "tcl")
    site = venv / "Lib" / "site-packages"
    if not site.is_dir():
        raise FileNotFoundError("The project's venv site-packages folder is missing.")
    copytree_filtered(site, runtime / "Lib" / "site-packages", {".git", ".github"})
    for name in ("python.exe", "pythonw.exe", "python311.dll"):
        if not (runtime / name).is_file():
            raise RuntimeError(f"Bundled runtime is missing {name}")
    return runtime / "python.exe", freeze_requirements(venv_python)

def copy_program(project: Path, payload_root: Path, stage: Path) -> None:
    app = stage / "app"
    app.mkdir(parents=True, exist_ok=True)
    source_fallback = source_fallback_root(payload_root)
    for relative in PROGRAM_ALLOWLIST:
        source = project / relative
        if not source.exists():
            source = project / "src" / relative
        if not source.exists():
            source = source_fallback / relative
        if not source.exists():
            raise FileNotFoundError(f"Required public program file is missing: {relative}")
        destination = app / relative
        if source.is_dir():
            copytree_filtered(source, destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    shutil.copy2(runtime_assets_root(payload_root) / "config.py", app / "config.py")
    shutil.copy2(locate_artist_list(project), app / "danbooru_artist_tags_v4.5.txt")

def copy_runtime_controls(payload_root: Path, stage: Path) -> None:
    runtime = stage / "runtime"
    assets = runtime_assets_root(payload_root)
    for name in RUNTIME_FILES:
        source = assets / name
        destination = runtime / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    (stage / "Launch Artist Ranker.cmd").write_text(
        '@echo off\r\n'
        'setlocal\r\n'
        f'title NovelAI Artist Ranker {APP_VERSION}\r\n'
        'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command '
        '"try { $response = Invoke-RestMethod -Uri \'http://127.0.0.1:7860/api/public/health\' '
        '-TimeoutSec 1; if ($response.ok) { exit 0 } } catch {}; exit 1"\r\n'
        'if not errorlevel 1 (\r\n'
        '  echo Artist Ranker is already running. Opening it now...\r\n'
        '  start "" "http://127.0.0.1:7860/ranker/"\r\n'
        '  timeout /t 3 /nobreak >nul\r\n'
        '  exit /b 0\r\n'
        ')\r\n'
        'set "PYTHONUTF8=1"\r\n'
        'set "PYTHONUNBUFFERED=1"\r\n'
        'cd /d "%~dp0app"\r\n'
        'echo Closing this window stops Artist Ranker.\r\n'
        'echo.\r\n'
        '"%~dp0runtime\\python.exe" "artist_elo_ranker_buffered.py"\r\n'
        'set "RANKER_EXIT=%ERRORLEVEL%"\r\n'
        'echo.\r\n'
        'echo Artist Ranker stopped with exit code %RANKER_EXIT%.\r\n'
        'pause\r\n'
        'exit /b %RANKER_EXIT%\r\n',
        encoding="utf-8")
    (stage / "Launch Artist Ranker in Background.cmd").write_text(
        '@echo off\r\nstart "" "%~dp0runtime\\pythonw.exe" "%~dp0runtime\\public_launcher.pyw"\r\n',
        encoding="utf-8")
    (stage / "Uninstall Artist Ranker.cmd").write_text(
        '@echo off\r\nstart "" "%~dp0runtime\\pythonw.exe" "%~dp0runtime\\uninstall.pyw"\r\n',
        encoding="utf-8")

def validate_stage(stage: Path) -> None:
    required = (
        "runtime/python.exe", "runtime/pythonw.exe", "runtime/public_launcher.pyw",
        "runtime/uninstall.pyw", "app/artist_elo_ranker_buffered.py",
        "app/danbooru_artist_tags_v4.5.txt", "artist-ranker.apk",
        "Launch Artist Ranker.cmd", "Launch Artist Ranker in Background.cmd",
        "Uninstall Artist Ranker.cmd",
    )
    for relative in required:
        path = stage / relative
        if not path.is_file():
            raise RuntimeError(f"Public release staging is missing {relative}")
    if (stage / "artist-ranker.apk").stat().st_size < 10_000:
        raise RuntimeError("Public release staging contains an invalid Android APK.")

def smoke_runtime(runtime_python: Path, stage: Path, fixture: bool) -> None:
    if fixture:
        return
    code = (
        "import sys;from pathlib import Path;"
        f"sys.path.insert(0,{str(stage/'app')!r});"
        "import gradio,fastapi,uvicorn,numpy,PIL,pydantic;"
        "compile(Path(sys.path[0]+'/artist_elo_ranker_buffered.py').read_text(encoding='utf-8'),'artist_elo_ranker_buffered.py','exec');"
        "print('PUBLIC_RUNTIME_SMOKE_OK')"
    )
    result = subprocess.run([str(runtime_python), "-I", "-c", code], cwd=str(stage), capture_output=True, text=True, timeout=120)
    if result.returncode != 0 or "PUBLIC_RUNTIME_SMOKE_OK" not in result.stdout:
        raise RuntimeError("Bundled runtime smoke test failed:\n" + result.stdout + "\n" + result.stderr)

def build_iexpress(payload_root: Path, stage: Path, release: Path, skip: bool) -> Path | None:
    if skip:
        return None
    if os.name != "nt":
        raise RuntimeError("IExpress Setup.exe can only be built on Windows.")
    iexpress = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "iexpress.exe"
    if not iexpress.is_file():
        raise FileNotFoundError("Windows IExpress was not found.")
    work = release / ".setup-work"
    work.mkdir(parents=True, exist_ok=True)
    payload_zip = work / "public_payload.zip"
    write_zip(stage, payload_zip)
    install_script = work / "install_public.ps1"
    shutil.copy2(runtime_assets_root(payload_root) / "install_public.ps1", install_script)
    setup = release / "NovelAI-Artist-Ranker-Setup.exe"
    sed = work / "artist-ranker.sed"
    sed.write_text(f"""[Version]
Class=IEXPRESS
SEDVersion=3
[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=0
HideExtractAnimation=1
UseLongFileName=1
InsideCompressed=0
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=
DisplayLicense=
FinishMessage=NovelAI Artist Ranker was installed.
TargetName={setup}
FriendlyName=NovelAI Artist Ranker {APP_VERSION}
AppLaunched=powershell.exe -NoProfile -ExecutionPolicy Bypass -File install_public.ps1 -Payload public_payload.zip
PostInstallCmd=<None>
AdminQuietInstCmd=
UserQuietInstCmd=
SourceFiles=SourceFiles
[SourceFiles]
SourceFiles0={work}\\
[SourceFiles0]
%FILE0%=
%FILE1%=
[Strings]
FILE0=install_public.ps1
FILE1=public_payload.zip
""", encoding="utf-8")
    subprocess.run([str(iexpress), "/N", str(sed)], check=True, cwd=str(work))
    if not setup.is_file() or setup.stat().st_size < 100_000:
        raise RuntimeError("IExpress did not create a valid Setup.exe.")
    shutil.rmtree(work)
    return setup

def copy_repo_template(payload_root: Path, release: Path, requirements: str) -> None:
    target = release / "public-repository"
    template, repository_mode = repository_template_root(payload_root)
    if repository_mode:
        copytree_filtered(
            template, target,
            {".git", "public-release", "venv", "android-builder", "downloads", "Data", "comparison_images"},
            skip_tests=False)
        (target / "requirements.lock.txt").write_text(requirements, encoding="utf-8")
        return
    copytree_filtered(template, target, skip_tests=False)
    (target / "requirements.lock.txt").write_text(requirements, encoding="utf-8")
    program = target / "src"
    program.mkdir(parents=True, exist_ok=True)
    source_root = source_fallback_root(payload_root)
    for relative in PROGRAM_ALLOWLIST:
        source = source_root / relative
        destination = program / relative
        if source.is_dir():
            copytree_filtered(source, destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    assets = runtime_assets_root(payload_root)
    shutil.copy2(assets / "config.py", program / "config.example.py")
    packaging_dir = target / "packaging"
    packaging_dir.mkdir(parents=True, exist_ok=True)
    for name in ("public_launcher.pyw", "uninstall.pyw", "install_public.ps1", "config.py"):
        shutil.copy2(assets / name, packaging_dir / name)
    shutil.copy2(Path(__file__).resolve(), packaging_dir / "build_public_release.py")

def build(project: Path, output: Path, *, skip_iexpress: bool = False, fixture_runtime: Path | None = None) -> Path:
    project = project.resolve()
    payload_root = Path(__file__).resolve().parent
    output.mkdir(parents=True, exist_ok=True)
    release = output / RELEASE_FOLDER
    if release.exists():
        shutil.rmtree(release)
    release.mkdir()
    with tempfile.TemporaryDirectory(prefix="artist-ranker-public-") as temp:
        stage = Path(temp) / "installed"
        stage.mkdir()
        runtime_python, requirements = copy_python_runtime(project, stage, fixture_runtime)
        copy_program(project, payload_root, stage)
        copy_runtime_controls(payload_root, stage)
        apk_source = project / "downloads" / "artist-ranker.apk"
        if not apk_source.is_file() or apk_source.stat().st_size < 10_000:
            raise FileNotFoundError("Build the signed Android APK before creating the public release.")
        shutil.copy2(apk_source, stage / "artist-ranker.apk")
        (stage / "requirements.lock.txt").write_text(requirements, encoding="utf-8")
        (stage / "VERSION.txt").write_text(f"{APP_VERSION}\n", encoding="utf-8")
        scan_private(stage)
        validate_stage(stage)
        smoke_runtime(runtime_python, stage, fixture_runtime is not None)
        portable = release / f"NovelAI-Artist-Ranker-Portable-v{APP_VERSION}.zip"
        write_zip(stage, portable)
        build_iexpress(payload_root, stage, release, skip_iexpress)
        shutil.copy2(apk_source, release / "artist-ranker.apk")
        copy_repo_template(payload_root, release, requirements)
    notes = release / "RELEASE_NOTES.md"
    notes.write_text(
        f"# NovelAI Artist Ranker {APP_VERSION}\n\n"
        "Phase 8 public distribution with portable portrait metadata, backward-compatible "
        "2.5.2 transfer restore, a visible live-log launcher that stops with its console, "
        "an optional background tray launcher, self-contained Windows runtime, safe uninstall, "
        "prebuilt Android app, locked dependencies, sanitized repository materials, and "
        "automated smoke/release workflows.\n",
        encoding="utf-8")
    files = [p for p in release.iterdir() if p.is_file() and p.name != "SHA256SUMS.txt"]
    sums = "\n".join(f"{sha256(path)}  {path.name}" for path in sorted(files)) + "\n"
    (release / "SHA256SUMS.txt").write_text(sums, encoding="utf-8")
    manifest = {
        "application": "NovelAI Artist Ranker",
        "version": APP_VERSION,
        "android_version": ANDROID_VERSION,
        "android_version_code": ANDROID_CODE,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "self_contained_windows_runtime": True,
        "credentials_included": False,
        "user_data_included": False,
        "files": {p.name: {"size": p.stat().st_size, "sha256": sha256(p)} for p in files},
    }
    (release / "release_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    scan_private(release)
    return release

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["build"])
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-iexpress", action="store_true")
    parser.add_argument("--fixture-runtime", type=Path)
    args = parser.parse_args()
    release = build(args.project, args.output, skip_iexpress=args.skip_iexpress, fixture_runtime=args.fixture_runtime)
    print(f"Public release created: {release}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
