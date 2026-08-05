#!/usr/bin/env python3
"""Build and verify the signed Artist Ranker APK on Windows.

This replaces the fragile batch/PowerShell bootstrap. It uses only Python's
standard library and keeps the Android project, signing identity, JDK, SDK, Gradle,
dependency cache, logs, and APK output in the permanent android-builder folder.
Fast builds are incremental; clean builds remain available for troubleshooting.
"""
from __future__ import annotations

import argparse
import hashlib
import locale
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterable, Optional, Sequence

BOOTSTRAP_VERSION = "1.8.0"
APP_PACKAGE = "com.sebas.artistranker"
APP_VERSION_NAME = "1.5.1"
APP_VERSION_CODE = "12"
MIN_SDK = "26"
COMPILE_SDK = "36"
TARGET_SDK = "36"
BUILD_TOOLS_VERSION = "36.0.0"
GRADLE_VERSION = "9.5.0"
AGP_VERSION = "9.3.0"
KEYSTORE_SHA256 = "f9735c8d3c261088e011da5096dfd6cf63b606ce527626a02c9ca51d6d49a484"

COMMANDLINE_TOOLS_URL = (
    "https://dl.google.com/android/repository/"
    "commandlinetools-win-15859902_latest.zip"
)
COMMANDLINE_TOOLS_SHA256 = (
    "90ae805d20434428bffcb699c290860f19bb5f66a67e6b330067e3de801fb04a"
)
GRADLE_URL = f"https://services.gradle.org/distributions/gradle-{GRADLE_VERSION}-bin.zip"
GRADLE_SHA256 = "553c78f50dafcd54d65b9a444649057857469edf836431389695608536d6b746"
JDK_URL = "https://aka.ms/download-jdk/microsoft-jdk-17-windows-x64.zip"
JDK_CHECKSUM_URL = JDK_URL + ".sha256sum.txt"

BUILDER_ROOT = Path(__file__).resolve().parent
RANKER_ROOT = BUILDER_ROOT.parent
PROJECT_DIR = BUILDER_ROOT / "project"
TOOLS_DIR = BUILDER_ROOT / "toolchain"
SDK_DIR = TOOLS_DIR / "android-sdk"
JDK_DIR = TOOLS_DIR / "microsoft-jdk-17"
GRADLE_DIR = TOOLS_DIR / f"gradle-{GRADLE_VERSION}"
GRADLE_USER_HOME = BUILDER_ROOT / "gradle-cache"
DOWNLOAD_DIR = TOOLS_DIR / "downloads"
LOG_DIR = BUILDER_ROOT / "logs"
LOG_PATH = LOG_DIR / "android_build_v1_8_0.log"
APK_SOURCE = PROJECT_DIR / "app" / "build" / "outputs" / "apk" / "release" / "app-release.apk"
APK_OUTPUT = BUILDER_ROOT / "output" / "artist-ranker.apk"
RANKER_APK_OUTPUT = RANKER_ROOT / "downloads" / "artist-ranker.apk"
SIGNING_KEY = BUILDER_ROOT / "signing" / "artist-ranker-release.jks"

ENCODING = locale.getpreferredencoding(False) or "utf-8"
_LOG_HANDLE = None


class BuildError(RuntimeError):
    pass


def log(message: str = "") -> None:
    text = str(message)
    print(text, flush=True)
    if _LOG_HANDLE is not None:
        _LOG_HANDLE.write(text + "\n")
        _LOG_HANDLE.flush()


def quote_command(command: Sequence[os.PathLike[str] | str]) -> str:
    return subprocess.list2cmdline([str(part) for part in command])


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def verify_sha256(path: Path, expected: str) -> None:
    actual = sha256_file(path)
    if actual != expected.strip().lower():
        raise BuildError(
            f"Checksum mismatch for {path.name}.\nExpected: {expected}\nActual:   {actual}"
        )


def download_file(url: str, destination: Path, expected_sha256: Optional[str] = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size > 0:
        if expected_sha256:
            try:
                verify_sha256(destination, expected_sha256)
                log(f"Reusing verified download: {destination.name}")
                return
            except BuildError:
                log(f"Cached download failed verification and will be replaced: {destination.name}")
                destination.unlink(missing_ok=True)
        else:
            log(f"Reusing cached download: {destination.name}")
            return

    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.unlink(missing_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "ArtistRankerBuilder/1.8.0"})
    log(f"Downloading {destination.name}...")
    last_error: Optional[BaseException] = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=90) as response, temporary.open("wb") as output:
                total_header = response.headers.get("Content-Length")
                total = int(total_header) if total_header and total_header.isdigit() else 0
                copied = 0
                last_report = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    copied += len(chunk)
                    if total and copied - last_report >= 25 * 1024 * 1024:
                        log(f"  {copied * 100 // total}% ({copied // (1024 * 1024)} MB)")
                        last_report = copied
            if temporary.stat().st_size == 0:
                raise BuildError(f"Downloaded file is empty: {url}")
            temporary.replace(destination)
            if expected_sha256:
                verify_sha256(destination, expected_sha256)
            return
        except (OSError, urllib.error.URLError, BuildError) as exc:
            last_error = exc
            temporary.unlink(missing_ok=True)
            if attempt < 3:
                log(f"Download attempt {attempt} failed: {exc}. Retrying...")
                time.sleep(attempt * 2)
    raise BuildError(f"Could not download {url}: {last_error}")


def download_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "ArtistRankerBuilder/1.8.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def safe_extract_zip(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive, "r") as bundle:
        for member in bundle.infolist():
            resolved = (destination / member.filename).resolve()
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise BuildError(f"Unsafe path in {archive.name}: {member.filename}") from exc
        bundle.extractall(destination)


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def run_capture(
    command: Sequence[os.PathLike[str] | str],
    *,
    cwd: Path = PROJECT_DIR,
    env: Optional[dict[str, str]] = None,
    input_text: Optional[str] = None,
    check: bool = True,
) -> tuple[int, str]:
    normalized = [str(part) for part in command]
    command_text = quote_command(normalized)
    log(f"> {command_text}")
    executable_command = normalized
    if normalized and Path(normalized[0]).suffix.casefold() in {".bat", ".cmd"}:
        comspec = (env or os.environ).get("COMSPEC", os.environ.get("COMSPEC", "cmd.exe"))
        executable_command = [comspec, "/d", "/s", "/c", command_text]
    completed = subprocess.run(
        executable_command,
        cwd=str(cwd),
        env=env,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding=ENCODING,
        errors="replace",
        timeout=None,
    )
    output = completed.stdout or ""
    if output:
        for line in output.rstrip().splitlines():
            log(line)
    if check and completed.returncode != 0:
        raise BuildError(f"Command failed with exit code {completed.returncode}: {command_text}")
    return completed.returncode, output


def parse_java_major(output: str) -> int:
    match = re.search(r'(?:java|openjdk) version\s+"([0-9]+)', output, re.IGNORECASE)
    if not match:
        match = re.search(r"\bversion\s+([0-9]+)(?:\.|\b)", output, re.IGNORECASE)
    return int(match.group(1)) if match else 0


def valid_java(java_executable: Path, minimum: int = 17) -> bool:
    if not java_executable.exists():
        return False
    try:
        code, output = run_capture([java_executable, "-version"], check=False)
        return code == 0 and parse_java_major(output) >= minimum
    except OSError:
        return False


def provision_jdk() -> Path:
    java_executable = JDK_DIR / "bin" / "java.exe"
    if valid_java(java_executable):
        log(f"Using cached private JDK: {JDK_DIR}")
        return java_executable

    remove_path(JDK_DIR)
    archive = DOWNLOAD_DIR / "microsoft-jdk-17-windows-x64.zip"
    checksum_file = DOWNLOAD_DIR / "microsoft-jdk-17-windows-x64.zip.sha256sum.txt"
    try:
        checksum_text = download_text(JDK_CHECKSUM_URL)
        checksum_file.parent.mkdir(parents=True, exist_ok=True)
        checksum_file.write_text(checksum_text, encoding="utf-8")
        match = re.search(r"\b([0-9a-fA-F]{64})\b", checksum_text)
        if not match:
            raise BuildError("Microsoft JDK checksum response did not contain a SHA-256 value.")
        expected = match.group(1).lower()
    except Exception as exc:
        raise BuildError(f"Could not retrieve the Microsoft JDK checksum: {exc}") from exc

    download_file(JDK_URL, archive, expected)
    with tempfile.TemporaryDirectory(prefix="artist-ranker-jdk-", dir=str(TOOLS_DIR)) as temporary_dir:
        extracted = Path(temporary_dir)
        safe_extract_zip(archive, extracted)
        candidates = sorted(extracted.rglob("bin/java.exe"), key=lambda path: len(path.parts))
        if not candidates:
            raise BuildError("The Microsoft JDK archive did not contain bin\\java.exe.")
        source_root = candidates[0].parent.parent
        shutil.copytree(source_root, JDK_DIR)

    if not valid_java(java_executable):
        raise BuildError(f"Private JDK installation is invalid: {java_executable}")
    return java_executable


def provision_commandline_tools() -> Path:
    sdkmanager = SDK_DIR / "cmdline-tools" / "latest" / "bin" / "sdkmanager.bat"
    if sdkmanager.exists():
        log(f"Using cached Android command-line tools: {sdkmanager}")
        return sdkmanager

    archive = DOWNLOAD_DIR / "commandlinetools-win-15859902_latest.zip"
    download_file(COMMANDLINE_TOOLS_URL, archive, COMMANDLINE_TOOLS_SHA256)
    latest_dir = SDK_DIR / "cmdline-tools" / "latest"
    remove_path(latest_dir)
    with tempfile.TemporaryDirectory(prefix="artist-ranker-sdktools-", dir=str(TOOLS_DIR)) as temporary_dir:
        extracted = Path(temporary_dir)
        safe_extract_zip(archive, extracted)
        candidates = sorted(extracted.rglob("bin/sdkmanager.bat"), key=lambda path: len(path.parts))
        if not candidates:
            raise BuildError("Android command-line tools archive did not contain sdkmanager.bat.")
        source_root = candidates[0].parent.parent
        latest_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_root, latest_dir)
    if not sdkmanager.exists():
        raise BuildError(f"Android command-line tools installation is invalid: {sdkmanager}")
    return sdkmanager


def provision_gradle() -> Path:
    gradle = GRADLE_DIR / "bin" / "gradle.bat"
    if gradle.exists():
        log(f"Using cached Gradle: {gradle}")
        return gradle

    archive = DOWNLOAD_DIR / f"gradle-{GRADLE_VERSION}-bin.zip"
    download_file(GRADLE_URL, archive, GRADLE_SHA256)
    remove_path(GRADLE_DIR)
    with tempfile.TemporaryDirectory(prefix="artist-ranker-gradle-", dir=str(TOOLS_DIR)) as temporary_dir:
        extracted = Path(temporary_dir)
        safe_extract_zip(archive, extracted)
        candidates = sorted(extracted.rglob("bin/gradle.bat"), key=lambda path: len(path.parts))
        if not candidates:
            raise BuildError("Gradle archive did not contain bin\\gradle.bat.")
        source_root = candidates[0].parent.parent
        shutil.copytree(source_root, GRADLE_DIR)
    if not gradle.exists():
        raise BuildError(f"Gradle installation is invalid: {gradle}")
    return gradle


def build_environment() -> dict[str, str]:
    env = dict(os.environ)
    env["JAVA_HOME"] = str(JDK_DIR)
    env["GRADLE_JAVA_HOME"] = str(JDK_DIR)
    env["ANDROID_SDK_ROOT"] = str(SDK_DIR)
    env["ANDROID_HOME"] = str(SDK_DIR)
    env["GRADLE_USER_HOME"] = str(GRADLE_USER_HOME)
    path_parts = [
        str(JDK_DIR / "bin"),
        str(SDK_DIR / "platform-tools"),
        str(SDK_DIR / "cmdline-tools" / "latest" / "bin"),
        env.get("PATH", ""),
    ]
    env["PATH"] = os.pathsep.join(path_parts)
    for name in ("JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "GRADLE_OPTS"):
        env.pop(name, None)
    return env


def android_sdk_required_files() -> list[Path]:
    return [
        SDK_DIR / "platforms" / f"android-{COMPILE_SDK}" / "android.jar",
        SDK_DIR / "build-tools" / BUILD_TOOLS_VERSION / "aapt2.exe",
        SDK_DIR / "build-tools" / BUILD_TOOLS_VERSION / "apksigner.bat",
        SDK_DIR / "platform-tools" / "adb.exe",
    ]


def android_sdk_ready() -> bool:
    return all(path.exists() for path in android_sdk_required_files())


def install_android_sdk(sdkmanager: Path, env: dict[str, str]) -> None:
    if android_sdk_ready():
        log(f"Using cached Android API {COMPILE_SDK} SDK and Build Tools {BUILD_TOOLS_VERSION}.")
        return

    packages = [
        "platform-tools",
        f"platforms;android-{COMPILE_SDK}",
        f"build-tools;{BUILD_TOOLS_VERSION}",
    ]
    log("\nAccepting Android SDK licenses...")
    run_capture(
        [sdkmanager, f"--sdk_root={SDK_DIR}", "--licenses"],
        env=env,
        input_text="y\n" * 200,
    )
    log("\nInstalling stable Android 16 SDK packages...")
    run_capture(
        [sdkmanager, f"--sdk_root={SDK_DIR}", "--channel=0", *packages],
        env=env,
    )

    missing = [str(path) for path in android_sdk_required_files() if not path.exists()]
    if missing:
        raise BuildError(
            "Android SDK installation returned success but required files are missing:\n- "
            + "\n- ".join(missing)
        )


def validate_project_source() -> None:
    app_gradle = (PROJECT_DIR / "app" / "build.gradle").read_text(encoding="utf-8")
    root_gradle = (PROJECT_DIR / "build.gradle").read_text(encoding="utf-8")
    manifest = (PROJECT_DIR / "app" / "src" / "main" / "AndroidManifest.xml").read_text(encoding="utf-8")
    java_source = (
        PROJECT_DIR
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "sebas"
        / "artistranker"
        / "MainActivity.java"
    ).read_text(encoding="utf-8")

    if not SIGNING_KEY.is_file():
        raise BuildError(f"The permanent signing key is missing: {SIGNING_KEY}")
    verify_sha256(SIGNING_KEY, KEYSTORE_SHA256)
    keystore_properties = PROJECT_DIR / "keystore.properties"
    if not keystore_properties.is_file():
        raise BuildError(f"The signing configuration is missing: {keystore_properties}")
    signing_text = keystore_properties.read_text(encoding="utf-8")
    if "storeFile=../signing/artist-ranker-release.jks" not in signing_text.replace("\\", "/"):
        raise BuildError("keystore.properties does not point to the permanent signing folder.")

    required_fragments = {
        "compileSdk": f"compileSdk {COMPILE_SDK}",
        "targetSdk": f"targetSdk {TARGET_SDK}",
        "minSdk": f"minSdk {MIN_SDK}",
        "package": f"applicationId '{APP_PACKAGE}'",
        "versionCode": f"versionCode {APP_VERSION_CODE}",
        "versionName": f"versionName '{APP_VERSION_NAME}'",
        "BuildConfig": "buildConfig true",
        "AGP": f"version '{AGP_VERSION}'",
        "cleartext": 'android:usesCleartextTraffic="true"',
        "network config": 'android:networkSecurityConfig="@xml/network_security_config"',
    }
    combined = app_gradle + "\n" + root_gradle + "\n" + manifest
    missing = [label for label, fragment in required_fragments.items() if fragment not in combined]
    if missing:
        raise BuildError("Project validation failed; missing markers: " + ", ".join(missing))
    if "android-37" in combined or "compileSdk 37" in combined:
        raise BuildError("Preview API 37 references remain in the project.")
    bridge_markers = (
        'addJavascriptInterface(new NativeBridge(), "ArtistRankerNative")',
        'public void haptic(String kind)',
        'public void setSoundEnabled(boolean enabled)',
        'public void bufferState(int ready, int target)',
    )
    missing_bridge = [marker for marker in bridge_markers if marker not in java_source]
    if missing_bridge:
        raise BuildError("The required narrow native bridge is incomplete: " + ", ".join(missing_bridge))
    if java_source.count("addJavascriptInterface(") != 1:
        raise BuildError("Unexpected additional JavaScript bridges were found in MainActivity.")

    guide_source_path = (
        PROJECT_DIR / "app" / "src" / "main" / "java" / "com" / "sebas"
        / "artistranker" / "NativeGuideOverlay.java"
    )
    guide_source = guide_source_path.read_text(encoding="utf-8")
    if "clipOutRoundRect" in guide_source:
        raise BuildError("NativeGuideOverlay uses the nonexistent Canvas.clipOutRoundRect API.")
    if "clipOutPath(cutoutPath)" not in guide_source or "addRoundRect" not in guide_source:
        raise BuildError("NativeGuideOverlay is missing the compatible rounded spotlight path.")


def write_local_properties() -> None:
    sdk_value = SDK_DIR.resolve().as_posix()
    (PROJECT_DIR / "local.properties").write_text(
        f"# Generated by Artist Ranker build bootstrap v{BOOTSTRAP_VERSION}\n"
        f"sdk.dir={sdk_value}\n",
        encoding="utf-8",
    )


def verify_apk_metadata(apk: Path, env: dict[str, str]) -> None:
    build_tools = SDK_DIR / "build-tools" / BUILD_TOOLS_VERSION
    apksigner = build_tools / "apksigner.bat"
    aapt2 = build_tools / "aapt2.exe"
    _, cert_output = run_capture(
        [apksigner, "verify", "--verbose", "--print-certs", apk], env=env
    )
    if "Verified" not in cert_output and "Signer #1 certificate" not in cert_output:
        raise BuildError("apksigner did not report a verified signer certificate.")

    _, badging = run_capture([aapt2, "dump", "badging", apk], env=env)
    checks = {
        "package name": f"name='{APP_PACKAGE}'",
        "versionCode": f"versionCode='{APP_VERSION_CODE}'",
        "versionName": f"versionName='{APP_VERSION_NAME}'",
        "target SDK": f"targetSdkVersion:'{TARGET_SDK}'",
    }
    missing = [label for label, marker in checks.items() if marker not in badging]
    minimum_sdk_patterns = (
        rf"(?:^|\n)sdkVersion:'{re.escape(MIN_SDK)}'(?:\n|$)",
        rf"(?:^|\n)minSdkVersion:'{re.escape(MIN_SDK)}'(?:\n|$)",
    )
    if not any(re.search(pattern, badging) for pattern in minimum_sdk_patterns):
        missing.append("minimum SDK")
    if missing:
        raise BuildError("APK metadata verification failed: " + ", ".join(missing))


def expose_apk(source: Path) -> None:
    APK_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    RANKER_APK_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, APK_OUTPUT)
    shutil.copy2(source, RANKER_APK_OUTPUT)
    expected = sha256_file(source)
    for copied in (APK_OUTPUT, RANKER_APK_OUTPUT):
        if sha256_file(copied) != expected:
            raise BuildError(f"The copied APK does not match the signed release APK: {copied}")


def recover_existing_apk() -> None:
    """Verify and expose the release APK Gradle already built on this PC."""
    if platform.system().lower() != "windows":
        raise BuildError("Existing-APK recovery must run on Windows.")
    validate_project_source()
    if not APK_SOURCE.exists() or APK_SOURCE.stat().st_size < 10_000:
        raise BuildError(
            "No previously built release APK was found at:\n"
            f"{APK_SOURCE}\nRun BUILD_APK_FAST.bat instead."
        )
    java = provision_jdk()
    env = build_environment()
    if not valid_java(java):
        raise BuildError(f"The private JDK is invalid: {java}")
    missing = [
        str(path)
        for path in (
            SDK_DIR / "build-tools" / BUILD_TOOLS_VERSION / "aapt2.exe",
            SDK_DIR / "build-tools" / BUILD_TOOLS_VERSION / "apksigner.bat",
        )
        if not path.exists()
    ]
    if missing:
        raise BuildError(
            "The APK exists, but verification tools are missing:\n- "
            + "\n- ".join(missing)
            + "\nRun BUILD_APK_FAST.bat once to install them."
        )
    log("\nRecovering the APK already built successfully by Gradle...")
    verify_apk_metadata(APK_SOURCE, env)
    expose_apk(APK_SOURCE)
    log("\nSUCCESS")
    log(f"Recovered signed APK: {RANKER_APK_OUTPUT}")
    log(f"SHA-256: {sha256_file(RANKER_APK_OUTPUT)}")
    log(f"Build log: {LOG_PATH}")


def perform_build(mode: str = "fast") -> None:
    if platform.system().lower() != "windows":
        raise BuildError("The real APK build must run on Windows. Use --self-test elsewhere.")
    if mode not in {"fast", "clean"}:
        raise BuildError(f"Unsupported build mode: {mode}")

    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    GRADLE_USER_HOME.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    validate_project_source()

    java = provision_jdk()
    sdkmanager = provision_commandline_tools()
    gradle = provision_gradle()
    env = build_environment()

    log("\nSelected permanent Java runtime:")
    log(f"JAVA_HOME={JDK_DIR}")
    _, java_output = run_capture([java, "-version"], env=env)
    if parse_java_major(java_output) < 17:
        raise BuildError("The selected private Java runtime is older than Java 17.")

    install_android_sdk(sdkmanager, env)
    write_local_properties()

    log("\nConfirming Gradle and Android Gradle Plugin prerequisites...")
    run_capture(
        [gradle, f"-Dorg.gradle.java.home={JDK_DIR}", "--no-daemon", "--version"],
        env=env,
    )

    tasks = ["assembleRelease"] if mode == "fast" else ["clean", "assembleRelease"]
    label = "incremental fast" if mode == "fast" else "clean troubleshooting"
    log(f"\nBuilding signed release APK ({label} build)...")
    run_capture(
        [
            gradle,
            f"-Dorg.gradle.java.home={JDK_DIR}",
            "--no-daemon",
            "--build-cache",
            "--stacktrace",
            *tasks,
        ],
        env=env,
    )
    if not APK_SOURCE.exists() or APK_SOURCE.stat().st_size < 10_000:
        raise BuildError(f"Gradle did not produce a valid release APK: {APK_SOURCE}")

    log("\nVerifying APK signature and manifest metadata...")
    verify_apk_metadata(APK_SOURCE, env)
    expose_apk(APK_SOURCE)

    log("\nSUCCESS")
    log(f"Signed APK installed for ranker download: {RANKER_APK_OUTPUT}")
    log(f"Permanent APK copy: {APK_OUTPUT}")
    log(f"SHA-256: {sha256_file(RANKER_APK_OUTPUT)}")
    log(f"Build log: {LOG_PATH}")


def verify_builder() -> None:
    validate_project_source()
    log(f"Permanent builder root: {BUILDER_ROOT}")
    log(f"Project: {PROJECT_DIR}")
    log(f"Signing key: {SIGNING_KEY}")
    log(f"Toolchain ready: {'yes' if (JDK_DIR / 'bin' / 'java.exe').exists() and android_sdk_ready() and (GRADLE_DIR / 'bin' / 'gradle.bat').exists() else 'not fully downloaded yet'}")
    if APK_OUTPUT.is_file():
        log(f"Permanent APK output: {APK_OUTPUT} ({APK_OUTPUT.stat().st_size:,} bytes)")
    if RANKER_APK_OUTPUT.is_file():
        log(f"Ranker download APK: {RANKER_APK_OUTPUT} ({RANKER_APK_OUTPUT.stat().st_size:,} bytes)")
    log("Permanent Android builder verification passed.")


def self_test() -> None:
    validate_project_source()
    assert PROJECT_DIR == BUILDER_ROOT / "project"
    assert TOOLS_DIR == BUILDER_ROOT / "toolchain"
    assert GRADLE_USER_HOME == BUILDER_ROOT / "gradle-cache"
    assert parse_java_major('openjdk version "17.0.12"') == 17
    assert parse_java_major('java version "21.0.1"') == 21
    assert parse_java_major("unrecognized") == 0

    with tempfile.TemporaryDirectory(prefix="artist-ranker-self-test-") as temporary_dir:
        base = Path(temporary_dir)
        harmless = base / "harmless.zip"
        with zipfile.ZipFile(harmless, "w") as bundle:
            bundle.writestr("root/file.txt", "ok")
        destination = base / "safe"
        safe_extract_zip(harmless, destination)
        assert (destination / "root" / "file.txt").read_text() == "ok"

        malicious = base / "malicious.zip"
        with zipfile.ZipFile(malicious, "w") as bundle:
            bundle.writestr("../escape.txt", "bad")
        try:
            safe_extract_zip(malicious, base / "unsafe")
        except BuildError:
            pass
        else:
            raise AssertionError("Unsafe ZIP traversal was not rejected")

    sample_new = "package: name='com.sebas.artistranker' versionCode='12' versionName='1.5.1'\nminSdkVersion:'26'\ntargetSdkVersion:'36'\n"
    sample_old = "package: name='com.sebas.artistranker' versionCode='12' versionName='1.5.1'\nsdkVersion:'26'\ntargetSdkVersion:'36'\n"
    minimum_patterns = (
        rf"(?:^|\n)sdkVersion:'{re.escape(MIN_SDK)}'(?:\n|$)",
        rf"(?:^|\n)minSdkVersion:'{re.escape(MIN_SDK)}'(?:\n|$)",
    )
    assert any(re.search(pattern, sample_new) for pattern in minimum_patterns)
    assert any(re.search(pattern, sample_old) for pattern in minimum_patterns)

    log("Artist Ranker Android build bootstrap self-test passed.")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true", help="Validate the permanent project and bootstrap without downloading tools.")
    parser.add_argument("--recover-existing", action="store_true", help="Verify and copy the APK already produced by Gradle.")
    parser.add_argument("--verify-only", action="store_true", help="Verify the permanent builder without building.")
    parser.add_argument("--mode", choices=("fast", "clean"), default="fast", help="Use incremental fast build or a full clean build.")
    args = parser.parse_args(argv)

    global _LOG_HANDLE
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    selected_log = LOG_DIR / ("builder_verify.log" if args.self_test or args.verify_only else LOG_PATH.name)
    _LOG_HANDLE = selected_log.open("w", encoding="utf-8", newline="\n")
    try:
        log(f"Artist Ranker permanent Android builder v{BOOTSTRAP_VERSION}")
        log(f"Builder root: {BUILDER_ROOT}")
        log(f"Project: {PROJECT_DIR}")
        log(f"Stable SDK: compile {COMPILE_SDK}, target {TARGET_SDK}, build tools {BUILD_TOOLS_VERSION}")
        if args.self_test:
            self_test()
        elif args.recover_existing:
            recover_existing_apk()
        elif args.verify_only:
            verify_builder()
        else:
            perform_build(args.mode)
        return 0
    except (BuildError, OSError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        log("\nBUILD FAILED")
        log(str(exc))
        log(f"Full log: {selected_log}")
        return 1
    finally:
        if _LOG_HANDLE is not None:
            _LOG_HANDLE.close()
            _LOG_HANDLE = None


if __name__ == "__main__":
    raise SystemExit(main())
