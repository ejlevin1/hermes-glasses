STATUS: READY-FOR-LIVE

# Lane gl-ios (continued as gl-ios-2 on eric-m4) — identity, Secrets, Cloudflare Access, fastlane, bridge fixes

Branch `ios/cf-access-fastlane`. Commits (oldest first):

| Commit | What |
|---|---|
| c2020e5 | (gl-ios, mbp-m2) identity `me.ericlevin.hermesglasses` / team `6MPMD6S5U9` on all 4 configs, `ITSAppUsesNonExemptEncryption = NO`, `scripts/write_secrets.sh`, .gitignore |
| 34b7cae | (gl-ios, mbp-m2) bridge F1 (token stripped from the hermes child env) + F2 (`--query=<text>`) + tests |
| 0696ae9 | feat(ios): Cloudflare Access headers + bridge token, Keychain, Settings fields, tests |
| 2d20914 | test: `scripts/run_swift_tests.sh` runs every standalone suite |
| ea8de3a | chore(release): fastlane certs/beta/test, Release manual signing, `CFBundleVersion = $(CURRENT_PROJECT_VERSION)`, docs |
| (this) | report |

Signing material is not on eric-m4: the match run and the signed archive/export are **deferred to parent s3 on
mbp-m2** (not a gap of this lane — see "Deferred to s3").

## Review of the previous session's commits

- c2020e5: complete against §1/§2. Bundle id + team on both target configs and both project configs; there is
  only ONE native target (no test target exists, so there are no test-target ids to change). Entitlements keep
  HotspotConfiguration + wifi-info, nothing added. `ITSAppUsesNonExemptEncryption` is `<false/>`. Icon
  re-checked here: `icon-1024.png` `hasAlpha: no`, 1024 px. `MWDAT.AppLinkURLScheme` / `CFBundleURLTypes` intact.
  DAT SDK still `exactVersion 0.8.0` — it builds on Xcode 26.2 (device + simulator) with no workaround needed.
- 34b7cae: complete against §5. Tests present and passing (below).
- One thing c2020e5 missed and this lane fixed: `Info.plist` had `CFBundleVersion` as the literal `1`, which
  overrides `CURRENT_PROJECT_VERSION` — the beta lane's "latest TestFlight + 1" would never have reached the
  bundle (every upload would be build 1 and the second one rejected). It is now `$(CURRENT_PROJECT_VERSION)`;
  verified: a Release build with `CURRENT_PROJECT_VERSION=7` produces `CFBundleVersion = 7`.

## What changed (paths)

- `HermesGlasses/Services/BridgeRequestBuilder.swift` (new, pure Foundation): `BridgeCredentials`,
  `BridgeRequestBuilder.url/request/failureHint`.
- `HermesGlasses/Services/BridgeCredentialStore.swift` (new): Keychain items `bridge_token`,
  `bridge_cf_access_client_id`, `bridge_cf_access_client_secret` (MapCredentials pattern, AfterFirstUnlock;
  logs field name + OSStatus only).
- `HermesGlasses/Services/HermesAPIClient.swift`: `init(endpoint:credentials:)`, socket opened with the built
  `URLRequest`; connect failures name the lock (403 → Cloudflare Access, close 4401 → bridge token, 404, 5xx).
  Error text uses the endpoint as typed, never the built URL (which carries the token).
- `HermesGlasses/ViewModels/HermesSessionViewModel.swift`: default endpoint for fresh installs
  `wss://glasses.ericlevin.me/voice` (a saved `hermes_endpoint` always wins); `makeAPIClient()` is the only
  constructor — session, launch probe `checkBridge()`, and Developer `testBridge()` all use it (Query / Photo /
  Visual in bridge mode go through the session's client, i.e. the same path); credential status + setters.
- `HermesGlasses/Views/SettingsView.swift`: Assistant → Bridge gets a "Bridge access" card.
- `HermesGlasses.xcodeproj/project.pbxproj`: 2 new sources registered; Release target config:
  `CODE_SIGN_STYLE = Manual`, `CODE_SIGN_IDENTITY[sdk=iphoneos*] = Apple Distribution`,
  `PROVISIONING_PROFILE_SPECIFIER[sdk=iphoneos*] = match AppStore me.ericlevin.hermesglasses`. Debug unchanged
  (Automatic).
- `HermesGlasses/Info.plist`: `CFBundleVersion = $(CURRENT_PROJECT_VERSION)`.
- `Gemfile`, `Gemfile.lock` (fastlane 2.241.2, lock copied from Tool Approvals), `.gitignore` (+ `.bundle/`).
- `fastlane/Appfile`, `Matchfile`, `Fastfile`, `.env.local.example`, `README.md` (generated).
- `scripts/run_swift_tests.sh` (new); `tests/bridge-request/main.swift` (new);
  `tests/device-context/main.swift`, `tests/display-logic/main.swift` (header now names the sources).
- `docs/RELEASE.md` (new), `README.md` (bridge-access step + Releasing section).

## PLAN §B checklist

- [x] B1 fork/clone/identity (worktree `git config user.email` = `ejlevin1@users.noreply.github.com`).
- [x] B2 bundle id / team on every config; DAT 0.8.0 exact, builds on Xcode 26.2.
- [x] B3 `Config/Secrets.xcconfig` copied from `~/Development/.env/hermes-glasses-secrets.xcconfig` (600,
  `git check-ignore` → `.gitignore:17`), never printed; `scripts/write_secrets.sh`; `beta` calls it when absent
  (fatal if the source is missing), `test` calls it best-effort. A Release build here has the MWDAT
  credentials substituted (checked as a boolean only).
- [x] B4 CF Access headers via `URLRequest` (both set → both headers; one missing → neither; never in the URL);
  `?token=` percent-encoded (RFC 3986 unreserved set, so `+ & = space / ? # %` survive the bridge's
  `parse_qs`), not duplicated when the endpoint already has `token`; empty fields = old URL exactly (LAN
  `ws://…:8765/voice` unchanged; legacy `?token=` endpoints keep working). Settings fields: Endpoint
  (default `wss://glasses.ericlevin.me/voice` for new installs), Bridge token (SecureField), CF Access Client ID
  (plain field, pre-filled from Keychain), CF Access Client Secret (SecureField). Secrets in Keychain only;
  committed on Done and swipe-dismiss (owned by root SettingsView). Developer "Bridge" button uses the same
  client constructor. Unit tests written and run.
- [x] B5 fastlane `certs` / `beta` (with `skip_upload:true`) / `test`, env adoption + keychain unlock as Tool
  Approvals, build number = latest TestFlight + 1 (`initial_build_number: 0`), `ASC_APP_ID` from env;
  `ITSAppUsesNonExemptEncryption = NO`; icon has no alpha (also asserted by `beta` before building).
  Match run: **deferred to s3** (see below).
- [x] B6 device build (generic/platform=iOS, `CODE_SIGNING_ALLOWED=NO`, Debug and Release) + simulator build;
  bridge tests; Swift tests. Signed archive/export: **deferred to s3**.

## Accessibility / UI notes (Bridge access card)

- Every new field has an `accessibilityLabel` and `accessibilityHint`; the SecureFields also expose
  `accessibilityValue` "Saved in Keychain" / "Not set" (VoiceOver never hears the secret).
- Fonts are text styles (`.body.monospaced()`, `.subheadline`) and the shared `fieldRow` label moved from a
  fixed 13 pt to `.footnote` (13 pt at default size), so the new rows scale with Dynamic Type.
- SecureFields are never pre-filled; placeholder says "Saved - type to replace" / "Not set"; empty = keep;
  a "Remove saved …" destructive button (≥44 pt) appears when something is saved. `textContentType`
  `.password` / `.username` / `.URL`, autocorrect + autocapitalisation off.
- Built from the existing Hermes primitives (`HermesSection`, `fieldRow`), one accent, no new colours.

## Tests

| Command | Result |
|---|---|
| `xcrun swiftc HermesGlasses/Services/BridgeRequestBuilder.swift tests/bridge-request/main.swift -o /tmp/bridge-request-tests && /tmp/bridge-request-tests` (targeted, new) | 38 PASS, 0 FAIL |
| `scripts/run_swift_tests.sh` (all standalone suites; also run by `fastlane test`) | **38 suites passed, 0 failed** |
| `cd bridge && .venv/bin/python -m unittest test_hermes_bridge -v` (Python 3.13 venv with websockets + edge-tts; system 3.9 has no websockets) | **Ran 39 tests, OK** (incl. F1 `test_bridge_token_is_not_in_the_child_environment`, `test_env_builder_is_a_plain_copy_when_no_token_is_set`, F2 `test_query_starting_with_dash_is_not_parsed_as_an_option`) |
| `bundle exec fastlane lanes` | parses; lanes certs / beta / test listed |
| `bundle exec fastlane test` | sim build SUCCEEDED + 38/38 suites, `fastlane.tools finished successfully` |
| `xcodebuild … -destination 'generic/platform=iOS Simulator' -derivedDataPath build/DerivedData -skipPackagePluginValidation -skipMacroValidation CODE_SIGNING_ALLOWED=NO build` | BUILD SUCCEEDED |
| same, `-destination 'generic/platform=iOS'` (Debug) | BUILD SUCCEEDED |
| same, `-configuration Release CURRENT_PROJECT_VERSION=7` | BUILD SUCCEEDED; bundle `CFBundleVersion = 7`; `-showBuildSettings`: Manual / Apple Distribution / `match AppStore me.ericlevin.hermesglasses` / `me.ericlevin.hermesglasses` / `6MPMD6S5U9` |

No XCTest target exists, so there is no xcresult. Only warning in the builds is pre-existing
(`HermesAPIClient.swift` Sendable capture in `sendFinalize`, untouched code).

Secret hygiene: before each commit `git diff --cached` was grepped for `AR|2315889662600412|`, `BEGIN PRIVATE KEY`,
`.p8`, `MATCH_PASSWORD=<value>`, filled `CLIENT_TOKEN`/`META_APP_ID` — no hits. New `Logger` lines log only
Keychain field names + OSStatus; no `print` in app code touches credentials.

## Deferred to s3 on mbp-m2 (signing material not on eric-m4)

1. `export PATH="/opt/homebrew/opt/ruby/bin:$PATH"; bundle config set --local path vendor/bundle; bundle install`
2. One-time: `MATCH_READONLY=0 bundle exec fastlane certs` (add `MATCH_DEVELOPMENT=1` if a dev profile is wanted
   for cable installs). Must reuse distribution `ZBZU9783S4` (and dev `76B22RR6W7`); abort if match says it is
   generating a certificate; watch for `Couldn't commit or push changes back to git`. Then record profile
   name/UUID/cert id and check `security cms -D -i <profile>` entitlement keys include
   `com.apple.developer.networking.HotspotConfiguration` and `com.apple.developer.networking.wifi-info`.
3. `bundle exec fastlane beta skip_upload:true` → signed `build/HermesGlasses.ipa`, verified by the lane
   (entitlements, build number, DAT credentials). Needs the ASC app record (for `latest_testflight_build_number`).
4. Release: `bundle exec fastlane beta` (optionally `ASC_APP_ID=<id>`).

## Deviations

- Signed device build, match run, archive/export: not run here (overrides for eric-m4); deferred as above.
- `fastlane/README.md` is fastlane's generated lane list (like Tool Approvals'); the human how-to is
  `docs/RELEASE.md`, because fastlane overwrites that README on every run.
- `test` lane runs the standalone `swiftc` suites (there is no XCTest target), via the new runner script.
- `certs` fetches `development` only with `MATCH_DEVELOPMENT=1` (spec: "only if needed"); device builds here
  are unsigned, and Release signing uses the appstore profile.
- `CFBundleShortVersionString` left as the literal `1.0` (project `MARKETING_VERSION = 0.4` is still ignored);
  not changed to avoid an unrequested visible version change.
- The skills `swiftui-expert-skill`, `ios-design-guidelines`, `ios-accessibility` are not installed in this
  session; their checklist (secure entry, labels/hints, Dynamic Type, VoiceOver values, 44 pt targets) was
  applied by hand.
- `.bundle/` gitignored (Tool Approvals does the same); `bundle config set --local path vendor/bundle` is a
  per-clone step, documented in docs/RELEASE.md.

## Open items / Proposed (not built)

- Set `ASC_APP_ID` (or turn it into a constant) once Eric creates the ASC app record.
- `CFBundleShortVersionString` → `$(MARKETING_VERSION)` and pick a real marketing version, if wanted.
- Pre-existing Sendable warning in `HermesAPIClient.sendFinalize`.
- Bridge test reachability probe runs on Settings dismiss and at launch against the default
  `wss://glasses.ericlevin.me/voice` for new installs — without the CF token saved it will show "unreachable"
  (403) until the Bridge access fields are filled. Expected; the failure text says so.
