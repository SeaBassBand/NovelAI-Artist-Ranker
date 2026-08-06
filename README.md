# NovelAI Artist Ranker

A local blind-comparison ranker for NovelAI artist tags, with desktop and paired Android interfaces.

## User installation

Download the current release assets:

- `NovelAI-Artist-Ranker-Setup.exe`
- `NovelAI-Artist-Ranker-Portable-v2.6.1.zip`
- `NovelAI-Artist-Ranker-Update-v2.6.1.zip`
- `artist-ranker.apk`
- `SHA256SUMS.txt`
- `RELEASE_NOTES.md`

The Windows packages include their own Python runtime. End users do not need Python, Git, Java, Gradle, or an Android SDK. The normal launcher keeps a visible log window open; closing that window stops the ranker. A background/tray launcher is included as an alternative.

An optional GitHub/Python installation is documented in [`SOURCE_INSTALL.md`](SOURCE_INSTALL.md). The comprehensive README rewrite is planned separately.

## First run

1. Install the Windows application.
2. Enter the NovelAI API key in the local Settings page.
3. Choose installed, portable, or custom data storage.
4. Configure retention and create an initial backup.
5. Pair the Android app with the one-use code.
6. Start ranking.

Complete exports store portable portrait references. When restored to another Data folder or computer, portrait and source-image paths are relocated automatically.

See `FOLDER_LAYOUT.md`, `BUILDING.md`, `SECURITY.md`, and `THIRD_PARTY_NOTICES.md`.
