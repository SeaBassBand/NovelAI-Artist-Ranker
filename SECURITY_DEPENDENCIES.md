# Dependency and supply-chain policy

Release builds use exact Python versions from `requirements.lock.txt`; Android and
Gradle versions remain explicit in the Android build files. Each release also includes
`DEPENDENCY_INVENTORY.json`, listing bundled Python versions and available license
metadata plus Android coordinates.

Dependabot checks Python and Gradle dependencies weekly. Proposed upgrades must pass
the smoke, packaging, large-library, backup, and Android build checks before release.

The Android signing keystore and its passwords are kept only in the private Development
compartment. They are never committed, copied into CI, or included in public packages.
