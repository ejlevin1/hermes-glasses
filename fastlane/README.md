fastlane documentation
----

# Installation

Make sure you have the latest version of the Xcode command line tools installed:

```sh
xcode-select --install
```

For _fastlane_ installation instructions, see [Installing _fastlane_](https://docs.fastlane.tools/#installing-fastlane)

# Available Actions

## iOS

### ios certs

```sh
[bundle exec] fastlane ios certs
```

Fetch (or, with MATCH_READONLY=0, create) the App Store signing profile

(+ development with MATCH_DEVELOPMENT=1). Read-only by default. Read

hermes-tool-approvals/ios/docs/BUILD_MACHINE.md before MATCH_READONLY=0.

### ios beta

```sh
[bundle exec] fastlane ios beta
```

Build and upload a build to TestFlight (internal testing only).

`skip_upload:true` stops after the signed archive + export + IPA check.

### ios test

```sh
[bundle exec] fastlane ios test
```

Build for the simulator (unsigned) and run every standalone Swift suite

in tests/ (scripts/run_swift_tests.sh). There is no XCTest target.

----

This README.md is auto-generated and will be re-generated every time [_fastlane_](https://fastlane.tools) is run.

More information about _fastlane_ can be found on [fastlane.tools](https://fastlane.tools).

The documentation of _fastlane_ can be found on [docs.fastlane.tools](https://docs.fastlane.tools).
