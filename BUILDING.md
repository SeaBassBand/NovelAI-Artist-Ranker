# Building releases

The supported release builder runs on Windows with the developer installation and its existing signed Android builder.

```bat
python packaging/build_public_release.py build --project . --output public-release
```

The builder:

1. Builds or consumes the signed Android APK.
2. Copies the current Python 3.11 runtime and locked site-packages.
3. Runs an isolated import/source smoke test.
4. Produces a portable ZIP.
5. Uses Windows IExpress to produce `NovelAI-Artist-Ranker-Setup.exe`.
6. Creates checksums, release notes, a manifest, and a sanitized repository snapshot.

Private keystores, signing properties, credentials, pairing state, user data, images, caches, and diagnostics are rejected by the release scan.


The developer checkout must provide `danbooru_artist_tags_v4.5.txt`, a Python 3.11 `venv`, and an already-signed `downloads\artist-ranker.apk`. Signing credentials remain outside the public repository.

The generated `public-repository` directory is a disposable publishing snapshot. Keep the permanent local Git clone elsewhere, copy the generated snapshot into that clone, review the changes, then commit and push. Do not initialize Git inside `public-release` because the next build replaces that directory.
