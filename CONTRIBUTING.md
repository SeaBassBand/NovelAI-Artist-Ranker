# Contributing

Use focused changes, preserve migration compatibility, and add a regression test for every bug fix. Never commit credentials, generated images, user data, Android signing files, toolchains, Gradle caches, or diagnostics.

Before opening a pull request:

```bash
python tests/smoke_test.py
```

For Android changes, run the permanent builder self-test and a signed release build on the maintainer's Windows environment.
