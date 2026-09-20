#!/bin/sh
# Proven-red gate for the local TDD loop (quality-gates).
#
#   sh prove-red.sh "<test command>"
#
# Runs the command against the current (pre-implementation) code and
# verifies the failure is real. A test that has never failed proves
# nothing; a failure that means "the runner never ran" proves even less.
#
# Exit codes:
#   0  a real red (with a warning when the red is a missing symbol only)
#   1  the test passed on current code: it cannot catch the bug
#   2  the runner did not run: broken command, collection error, no tests
set -u

cmd="${1:?usage: prove-red.sh \"<test command>\"}"

out=$(sh -c "$cmd" 2>&1); code=$?

# Zero tests ran, whatever the exit code says. "0 passing" with exit 0 is
# not a passing test suite; it is a check that measured nothing.
NO_TESTS_RE='collected 0 items|no tests ran|No tests ran|No tests found|No test files found|file or directory not found|Ran 0 tests|0 passing'

if [ "$code" -eq 0 ]; then
  if printf '%s\n' "$out" | grep -qE "$NO_TESTS_RE"; then
    echo "❌ The command exited 0 but ran no tests."
    echo "   A suite that ran nothing proves nothing. Check the path, markers"
    echo "   and test names."
    echo "----- command output (tail) -----"
    printf '%s\n' "$out" | tail -15
    exit 2
  fi
  echo "❌ Proven red failed: the test passed on current code."
  echo "   It cannot catch the bug it claims to guard. Rewrite the test"
  echo "   or run it before implementing the change."
  exit 1
fi

case "$code" in
  126|127)
    echo "❌ The test command could not be run (exit $code): missing binary or no permission."
    exit 2 ;;
esac
if [ "$code" -gt 128 ]; then
  echo "❌ The test command was killed (exit $code). Not a red test."
  exit 2
fi

# A collection error, not a red test: nothing was selected to run.
if printf '%s\n' "$out" | grep -qE "$NO_TESTS_RE"; then
  echo "❌ No tests were collected. Check the path, markers and test names."
  echo "----- command output (tail) -----"
  printf '%s\n' "$out" | tail -15
  exit 2
fi

# Evidence the runner started at all. A non-zero exit without it is a
# broken command, not a test going red.
RAN_RE='(passed|failed|error|FAIL|PASS|Ran [0-9]+ test|collected [1-9][0-9]* items?|Tests? run|Test Files|assertion)'
if ! printf '%s\n' "$out" | grep -qE "$RAN_RE"; then
  echo "❌ The command exited $code with no sign the test runner started."
  echo "   That is a broken command, not a red test."
  echo "----- command output (tail) -----"
  printf '%s\n' "$out" | tail -15
  exit 2
fi

# A red made only of missing symbols proves the code is new, not that the
# assertions bite. An empty test would fall the same way.
MISSING_RE='ImportError|ModuleNotFoundError|AttributeError:|NameError:|is not a function|Cannot find module|No such file or directory|has no attribute'
ASSERTION_RE='AssertionError|AssertionFailedError|assertion failed|AssertionError:|assert |assert\(|assert_eq|expected|Expected'
if printf '%s\n' "$out" | grep -qE "$MISSING_RE" && ! printf '%s\n' "$out" | grep -qE "$ASSERTION_RE"; then
  echo "⚠️  The red is a missing symbol only: the test fell because the code it"
  echo "   references does not exist yet. Legitimate for new code, but check"
  echo "   that the assertions actually bite — an empty test would fall too."
fi

echo "✅ Proven red: the test failed on current code, as it should."
