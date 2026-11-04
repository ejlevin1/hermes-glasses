#!/usr/bin/env bash
#
# run_swift_tests.sh - compile and run every standalone Swift suite in tests/.
#
# There is no XCTest target: each tests/<suite>/main.swift is compiled with
# swiftc together with the app sources it exercises. Those sources are read
# from the suite's OWN header comment (every `HermesGlasses/....swift` path in
# the leading `//` block), which is the build command CONTRIBUTING.md and each
# suite document - so this runner can never disagree with the documentation.
#
#   scripts/run_swift_tests.sh              # every suite
#   scripts/run_swift_tests.sh bridge-request providers   # just these
#
# Exit status is non-zero if any suite fails to compile or fails a test.
# Used by the fastlane `test` lane.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
OUT="${TMPDIR:-/tmp}/hermes-swift-tests"
mkdir -p "$OUT"

if [ "$#" -gt 0 ]; then
  suites=("$@")
else
  suites=()
  for d in tests/*/; do
    [ -f "$d/main.swift" ] && suites+=("$(basename "$d")")
  done
fi

passed=0
failed=()
for suite in "${suites[@]}"; do
  main="tests/$suite/main.swift"
  if [ ! -f "$main" ]; then
    echo "!! $suite: no $main"; failed+=("$suite"); continue
  fi
  # Leading comment block only: stop at the first line that is not a comment.
  sources=$(awk '/^[[:space:]]*\/\// { print; next } { exit }' "$main" \
    | grep -oE 'HermesGlasses/[A-Za-z0-9_/+.-]+\.swift' | awk '!seen[$0]++')
  # shellcheck disable=SC2086
  if ! xcrun swiftc -suppress-warnings $sources "$main" -o "$OUT/$suite" >"$OUT/$suite.build.log" 2>&1; then
    echo "!! $suite: COMPILE FAILED (log: $OUT/$suite.build.log)"
    tail -20 "$OUT/$suite.build.log"
    failed+=("$suite"); continue
  fi
  if (cd "$REPO_ROOT" && "$OUT/$suite") >"$OUT/$suite.log" 2>&1; then
    echo "ok $suite ($(grep -c '^PASS\|^  ✓\|^✓' "$OUT/$suite.log" || true) checks reported)"
    passed=$((passed + 1))
  else
    echo "!! $suite: FAILED (log: $OUT/$suite.log)"
    grep -E 'FAIL|✗' "$OUT/$suite.log" | head -20
    failed+=("$suite")
  fi
done

echo
echo "$passed passed, ${#failed[@]} failed (of ${#suites[@]} suites)"
[ "${#failed[@]}" -eq 0 ] || { echo "failed: ${failed[*]}"; exit 1; }
