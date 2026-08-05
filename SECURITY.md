# Security policy

## Supported versions

Only the newest public release receives security fixes.

## Reporting

Do not open public issues containing API keys, pairing tokens, private paths, generated private images, or signing material. Report security concerns privately to the project maintainer.

## Local security model

- NovelAI credentials are stored through Windows Credential Manager.
- Secret-management and launcher-control endpoints accept only local-PC requests.
- Phone access requires one-use pairing and per-device credentials.
- Release packages exclude credentials, pairing state, signing files, user data, images, diagnostics, and caches.
- Import/update archives are path-validated and SHA-256 verified before application.
