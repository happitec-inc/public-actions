#!/usr/bin/env bash
# Tests for bump-tap-formula's NAME RESOLUTION guards (cask name resolution).
#
# Like tests/bump-tap-url-style.sh, this EXTRACTS the shipped logic from
# .github/workflows/bump-tap-formula.yml rather than restating it, so the test
# cannot drift from the code. If the extraction stops finding what it expects it
# fails loudly instead of quietly testing nothing.
#
# WHAT IS BEING PROTECTED
#
# `formula:` defaults to the caller's repo name. That is usually right for a
# formula and wrong for every cask in the tap, because a cask is named for the
# app:
#     example-app-repo       -> Casks/example-app-repo.rb        (real: example-app)
#     example-receiver-repo  -> Casks/example-receiver-repo.rb   (real: example-cask)
# 0-for-2. It fails loudly, but it fails at TAG PUSH inside a release, and the
# old error text led with "initial registration must happen" -- sending the
# reader to the tap when the cause is in the caller.
#
# Usage: bash tests/bump-tap-name-resolution.sh
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

WF=.github/workflows/bump-tap-formula.yml
[ -f "$WF" ] || { echo "cannot find $WF"; exit 1; }

pass=0; fail=0
check() { # <desc> <expected> <got>
  if [ "$3" = "$2" ]; then pass=$((pass+1)); printf '  ok   %s\n' "$1"
  else fail=$((fail+1)); printf '  FAIL %-56s expected=%s got=%s\n' "$1" "$2" "$3"; fi
}

# ---- extract the cask-name guard, ANCHORED TO ITS LOCATION ---------------
# An earlier version of this extractor grepped the whole file for the guard's
# text. That pins the text EXISTING, not the guard RUNNING: a reviewer moved the
# guard into the Summary step -- valid YAML, dead code, workflow behaves as
# though unguarded -- and this suite still reported 7/7. Every mutation used to
# develop it varied the guard IN PLACE, so none of them could have caught
# relocation. Same blind spot as the extractor in bump-tap-url-style.sh.
#
# So the extraction is now anchored twice:
#   1. it must come from the "Resolve inputs" step's run: block, not the file;
#   2. it must appear BEFORE the FORMULA= assignment it protects.
# A guard that is moved, or that runs after the value it guards, now fails here.

# 1. narrow to the Resolve inputs step (from its `run: |` to the next step's `- name:`)
STEP=$(awk '/^      - name: Resolve inputs$/{f=1} f&&/^        run: \|$/{r=1;next} f&&r&&/^      - name: /{exit} f&&r{print}' "$WF")
if [ -z "$STEP" ]; then
  echo "  FAIL could not isolate the 'Resolve inputs' step in $WF."
  echo "       The step may have been renamed. Fix the extractor before trusting this suite."
  exit 1
fi

# 2. the guard must live inside that step
GUARD=$(printf '%s\n' "$STEP" | awk '/if \[ "\$KIND" = "cask" \] && \[ -z "\$INPUT_FORMULA" \]; then/,/^ *fi[[:space:]]*$/')
if [ -z "$GUARD" ]; then
  echo "  FAIL the cask-name guard is not in the 'Resolve inputs' step."
  echo "       It may have been deleted, or MOVED somewhere it does not run."
  exit 1
fi

# 3. and it must precede the assignment it protects, or it guards nothing
# shellcheck disable=SC2016  # these are literal grep patterns, not expansions
GUARD_LINE=$(printf '%s\n' "$STEP" | grep -n 'if \[ "\$KIND" = "cask" \] && \[ -z "\$INPUT_FORMULA" \]; then' | head -1 | cut -d: -f1)
# shellcheck disable=SC2016  # literal grep pattern
ASSIGN_LINE=$(printf '%s\n' "$STEP" | grep -n 'FORMULA="\${INPUT_FORMULA:-\$REPO_NAME}"' | head -1 | cut -d: -f1)
if [ -z "$GUARD_LINE" ] || [ -z "$ASSIGN_LINE" ]; then
  echo "  FAIL could not locate both the guard and the FORMULA= assignment in the step."; exit 1
fi
if [ "$GUARD_LINE" -ge "$ASSIGN_LINE" ]; then
  echo "  FAIL the guard is at line $GUARD_LINE but the FORMULA= assignment is at $ASSIGN_LINE."
  echo "       A guard that runs after the value it guards protects nothing."
  exit 1
fi

# decide: prints REFUSE (guard fires) or ACCEPT
decide() { # <kind> <input_formula>
  KIND="$1" INPUT_FORMULA="$2" REPO_NAME="example-app-repo" \
  bash -c "$GUARD"'; echo ACCEPT' 2>/dev/null | tail -1 | grep -q ACCEPT && echo ACCEPT || echo REFUSE
}

echo "== the cask trap: a name nobody supplied =="
check "cask with no formula: is refused"            REFUSE "$(decide cask '')"
check "cask WITH formula: proceeds"                 ACCEPT "$(decide cask example-app)"

echo "== formulas keep the repo-name default =="
# The default is usually correct for formulas (example-formula ships Formula/example-formula.rb),
# and tightening it there would break every existing caller for no benefit.
check "formula with no formula: still proceeds"     ACCEPT "$(decide formula '')"
check "formula WITH formula: proceeds"              ACCEPT "$(decide formula example-formula)"

echo "== the refusal must SAY the name was defaulted =="
MSG=$(KIND=cask INPUT_FORMULA='' REPO_NAME='example-app-repo' bash -c "$GUARD" 2>&1 | head -1)
case "$MSG" in
  *"defaulted to the repo name"*) pass=$((pass+1)); printf '  ok   names the defaulting as the cause\n' ;;
  *) fail=$((fail+1)); printf '  FAIL refusal does not name the defaulting: %s\n' "$MSG" ;;
esac
case "$MSG" in
  *"example-app-repo"*) pass=$((pass+1)); printf '  ok   shows the name it would have used\n' ;;
  *) fail=$((fail+1)); printf '  FAIL refusal does not show the resolved name: %s\n' "$MSG" ;;
esac

echo "== the not-found message must name the right cause =="
# Item 2 of the PR had no test. The arms are selected by NAME_DEFAULTED, so
# extract the branch and drive it directly.
NF=$(awk '/if \[ "\$NAME_DEFAULTED" = "true" \]; then/,/^ *fi[[:space:]]*$/' "$WF")
if [ -z "$NF" ]; then
  echo "  FAIL could not extract the not-found message branch"; exit 1
fi
msg() { NAME_DEFAULTED="$1" KIND=cask FORMULA_PATH=tap/Casks/x.rb bash -c "$NF" 2>&1 | head -1; }
case "$(msg true)" in
  *"defaulted to the repo name"*) pass=$((pass+1)); printf '  ok   defaulted name -> says it defaulted\n' ;;
  *) fail=$((fail+1)); printf '  FAIL defaulted arm does not name the defaulting\n' ;;
esac
case "$(msg false)" in
  *"typo"*) pass=$((pass+1)); printf '  ok   supplied name -> points at a typo\n' ;;
  *) fail=$((fail+1)); printf '  FAIL supplied arm does not suggest a typo\n' ;;
esac
# Both arms must keep the kind: hint -- a first-time caller who passed neither
# kind: nor formula: is exactly the wiring mistake that produced cask name resolution.
for d in true false; do
  case "$(msg $d)" in
    *"kind:"*) pass=$((pass+1)); printf '  ok   NAME_DEFAULTED=%s arm keeps the kind: hint\n' "$d" ;;
    *) fail=$((fail+1)); printf '  FAIL NAME_DEFAULTED=%s arm dropped the kind: hint\n' "$d" ;;
  esac
done

echo "== the skip path must announce itself where people look =="
# A skip exits 0 with the PR step gated off, so the job goes green having opened
# no PR. Without an annotation the only trace is the step summary, which is not
# where anyone looks first -- and the observer concludes the automation never ran.
if grep -q '::notice::No PR opened' "$WF"; then
  pass=$((pass+1)); printf '  ok   skip emits a ::notice:: annotation\n'
else
  fail=$((fail+1)); printf '  FAIL skip has no ::notice:: annotation\n'
fi
# There are TWO skip paths and they compare different things: asset-download
# compares a sha256, git-checkout compares a revision. The Summary step cannot
# tell them apart (MODE is not one of its inputs), so naming either mechanism
# is a signal that is wrong half the time -- the very defect this suite guards.
if grep -n '::notice::No PR opened' "$WF" | grep -qE 'sha256|revision'; then
  fail=$((fail+1)); printf '  FAIL the skip annotation names a compare mechanism it cannot know\n'
else
  pass=$((pass+1)); printf '  ok   skip annotation names no mechanism (two skip paths differ)\n'
fi

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass/$((pass+fail))"; else echo "FAIL — $fail of $((pass+fail)) failed"; fi
[ "$fail" -eq 0 ]
