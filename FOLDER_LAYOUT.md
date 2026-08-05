# Recommended folder layout

Keep the application, private user data, Git repository, backups, and private build environment separate. For example:

```text
D:\Storage\
├── Programs\NovelAI-Artist-Ranker\
├── NovelAI-Artist-Ranker-Data\
├── Repositories\NovelAI-Artist-Ranker\
├── Backups\NovelAI-Artist-Ranker\
└── Development\NovelAI-Artist-Ranker-Private\
```

## Programs

Contains an extracted portable release or installed runtime. It is replaceable and is not a Git repository.

## Data

Contains rankings, duel history, prompts, settings, generated images, artist portraits, diagnostics, and recovery state. Choose this as a Custom folder in the ranker. Never commit it to Git.

## Repositories

Contains the permanent local clone of the public GitHub repository. Source changes are committed here and pushed to GitHub. It must not contain personal Data, credentials, signing keys, or release caches.

## Backups

Contains complete profile-export ZIP files copied out of the active Data folder. These are private and must not be attached to a GitHub Release.

## Development

Contains the maintainer-only release environment, Android signing identity, and any local toolchains needed to create signed releases. It is private and is not mirrored to the public repository.

The generated `public-release\...\public-repository` folder is a sanitized publishing snapshot, not the permanent Git checkout. Sync it into the repository clone, review it, commit it, and push it. GitHub Release assets such as Setup, Portable ZIP, and APK are uploaded separately from repository source files.
