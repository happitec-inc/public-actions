#!/usr/bin/env bash
# Tests for the bump-tap-formula downgrade guard.
#
# Unlike earlier revisions that kept a duplicate copy of the decision logic,
# this script EXTRACTS the decision comparison straight from
# .github/workflows/bump-tap-formula.yml and executes it against fixture pairs.
# If the comparison logic in the workflow drifts, breaks, or switches away from
# sort -V, this suite immediately detects the regression.
#
# Usage: bash tests/bump-tap-downgrade-guard.sh
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

WF="${WF:-.github/workflows/bump-tap-formula.yml}"
[ -f "$WF" ] || { echo "cannot find $WF"; exit 1; }

WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT

# Extract the downgrade comparison block (from the first if check to the 3rd fi)
python3 - "$WF" "$WORK/guard_decision.sh" <<'PY'
import re, sys
wf, out = sys.argv[1], sys.argv[2]
src = open(wf, encoding="utf-8").read()
match = re.search(r'(if \[ -n "\$GUARD_OLD" \] && \[ "\$GUARD_OLD" != "\$NEW_VERSION" \]; then.*?fi\s+fi\s+fi)', src, re.DOTALL)
if not match:
    sys.stderr.write("Failed to extract downgrade comparison block from workflow\n")
    sys.exit(2)
open(out, "w", encoding="utf-8").write(match.group(1))
PY
[ -s "$WORK/guard_decision.sh" ] || { echo "extraction produced nothing"; exit 1; }
GUARD_CODE=$(cat "$WORK/guard_decision.sh")

# Mirrors the guard by running the extracted block in an isolated subshell:
# prints REFUSE / ALLOW-DOWNGRADE / PROCEED.
decide() {
  local GUARD_OLD="$1" NEW_VERSION="$2" ALLOW_DOWNGRADE="$3"
  local OUT
  OUT=$(GITHUB_STEP_SUMMARY=/dev/null FORMULA="fixture-formula" TAG="v${NEW_VERSION}" \
    GUARD_OLD="$GUARD_OLD" NEW_VERSION="$NEW_VERSION" ALLOW_DOWNGRADE="$ALLOW_DOWNGRADE" \
    bash -c "$GUARD_CODE" 2>&1)
  local RC=$?
  if [ "$RC" -ne 0 ]; then
    echo "REFUSE"
  elif echo "$OUT" | grep -q "DOWNGRADE (deliberate)"; then
    echo "ALLOW-DOWNGRADE"
  else
    echo "PROCEED"
  fi
}

pass=0; fail=0
check() { # <desc> <old> <new> <allow> <expected>
  local desc="$1" got
  got=$(decide "$2" "$3" "$4")
  if [ "$got" = "$5" ]; then
    pass=$((pass+1)); printf '  ok   %-58s %s\n' "$desc" "$got"
  else
    fail=$((fail+1)); printf '  FAIL %-58s expected=%s got=%s\n' "$desc" "$5" "$got"
  fi
}

echo "== downgrade guard incident fixture =="
check "0.2.1 -> 0.1.40 (accidental downgrade)"           "0.2.1"  "0.1.40" false REFUSE
check "0.2.1 -> 0.1.40 with allow-downgrade"              "0.2.1"  "0.1.40" true  ALLOW-DOWNGRADE

echo "== why sort -V (not lexicographic) =="
# sort -V is required because lexicographic string compare breaks on multi-digit components:
# "0.1.10" < "0.1.9" lexicographically, which would falsely refuse valid bumps like 0.1.9 -> 0.1.10.
check "0.1.9  -> 0.1.10 (multi-digit patch bump)"          "0.1.9"  "0.1.10" false PROCEED
check "0.9.0  -> 0.10.0 (multi-digit minor bump)"          "0.9.0"  "0.10.0" false PROCEED
check "0.1.10 -> 0.1.9  (genuine patch downgrade)"         "0.1.10" "0.1.9"  false REFUSE
# Demonstrate the naive comparison really is wrong on the first fixture:
if [ "0.1.10" \< "0.1.9" ]; then
  echo "  ok   lexicographic WOULD wrongly refuse 0.1.9 -> 0.1.10 — sort -V required"
else
  echo "  !!   lexicographic agrees here; re-check the justification for sort -V"
fi

echo "== normal forward bumps =="
check "0.1.39 -> 0.1.40"                                   "0.1.39" "0.1.40" false PROCEED
check "0.2.1  -> 0.2.2"                                    "0.2.1"  "0.2.2"  false PROCEED
check "0.1.40 -> 0.2.0 (minor rollover)"                  "0.1.40" "0.2.0"  false PROCEED
check "0.9.9  -> 1.0.0 (major rollover)"                  "0.9.9"  "1.0.0"  false PROCEED
check "1.2.3  -> 1.10.0 (double-digit minor)"             "1.2.3"  "1.10.0" false PROCEED

echo "== equal versions fall through (asset re-sign must still re-bump) =="
check "0.2.2 -> 0.2.2"                                    "0.2.2"  "0.2.2"  false PROCEED
check "0.2.2 -> 0.2.2 with allow-downgrade"               "0.2.2"  "0.2.2"  true  PROCEED

echo "== other downgrades =="
check "1.0.0  -> 0.9.9"                                   "1.0.0"  "0.9.9"  false REFUSE
check "0.1.40 -> 0.1.39 (patch back)"                     "0.1.40" "0.1.39" false REFUSE
check "0.2.10 -> 0.2.9 (double-digit patch back)"         "0.2.10" "0.2.9"  false REFUSE

echo
if [ "$fail" -eq 0 ]; then
  echo "PASS — $pass/$((pass+fail))"
else
  echo "FAIL — $fail of $((pass+fail)) failed"
fi
[ "$fail" -eq 0 ]
