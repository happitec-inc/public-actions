#!/usr/bin/env bash
# Tests that bump-tap-formula resets Homebrew's PACKAGING `revision` on a bump.
#
# Like tests/bump-tap-url-style.sh, this EXTRACTS the rewriters straight out of
# .github/workflows/bump-tap-formula.yml rather than keeping a copy. A copy
# drifts the moment someone edits one and not the other.
#
# What is being protected (packaging revision reset on version bump):
#   A top-level `revision N` is Homebrew's packaging revision — the thing that
#   makes a packaging-only change outrank the same upstream version. The DSL
#   expects it to RESET when the version moves. The rewriter did three
#   substitutions (tag:, revision: SHA, version) and a bare `revision N`
#   matched none of them, so once a formula carried `revision 1` it emitted
#   0.3.1_1, 0.3.2_1, ... permanently.
#
#   The trap in fixing it: `revision N` (packaging) and `revision: "<sha>"`
#   (the git pin) share a word. A regex loose enough to catch the first and
#   also catch the second would silently destroy the source pin — which is why
#   THE PIN-SURVIVAL ASSERTION BELOW IS THE POINT OF THIS FILE, not a bonus.
#
# Usage: bash tests/bump-tap-revision-reset.sh
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

WF=.github/workflows/bump-tap-formula.yml
[ -f "$WF" ] || { echo "cannot find $WF"; exit 1; }

WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ok   — $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL — $1"; }

# ---------------------------------------------------------------------------
# Extract both rewriter heredocs. Anchored on the `python3 - "$FORMULA_PATH"`
# invocation that actually runs them, NOT on surrounding prose — a snippet that
# is never executed must not be able to satisfy this extractor.
# ---------------------------------------------------------------------------
python3 - "$WF" "$WORK" <<'PY'
import re, sys, textwrap
wf, out = sys.argv[1], sys.argv[2]
src = open(wf, encoding='utf-8').read()

# Each live rewriter is `python3 - <args> <<'PY' ... PY` inside a run: block.
blocks = re.findall(r"python3 - \"\$FORMULA_PATH\".*?<<'PY'\n(.*?)\n\s*PY\n", src, re.S)
if len(blocks) != 2:
    sys.stderr.write(
        f"expected exactly 2 executed rewriter heredocs, found {len(blocks)}. "
        "If a third was added or one moved, this extractor is testing the wrong "
        "thing and must be updated rather than loosened.\n")
    sys.exit(1)
for name, body in zip(('git.py', 'asset.py'), blocks):
    open(f"{out}/{name}", 'w', encoding='utf-8').write(textwrap.dedent(body) + "\n")
print("extracted 2 rewriters")
PY
[ $? -eq 0 ] || { echo "extraction failed"; exit 1; }

# ---------------------------------------------------------------------------
# 1. git-checkout: packaging revision dropped, git revision: pin SURVIVES
# ---------------------------------------------------------------------------
cat > "$WORK/git.rb" <<'RB'
class DashServices < Formula
  url "https://github.com/happitec-inc/dash-services.git",
      tag:      "v0.3.0",
      revision: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  version "0.3.0"
  revision 1
  license "MIT"
end
RB
python3 "$WORK/git.py" "$WORK/git.rb" v0.4.0 bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb 0.4.0 1 >/dev/null 2>&1
rc=$?
[ $rc -eq 0 ] && ok "git-checkout rewriter exits 0" || bad "git-checkout rewriter exited $rc"

grep -qE '^\s*revision 1\s*$' "$WORK/git.rb" \
  && bad "packaging 'revision 1' still present — it was NOT reset" \
  || ok "packaging 'revision 1' dropped"

# THE DISCRIMINATOR. An over-broad regex would delete this line, and every
# other assertion in this file would still pass while the formula became
# unbuildable. If this ever fails, the fix is wrong, not the test.
grep -qE '^\s*revision: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"' "$WORK/git.rb" \
  && ok "git 'revision:' SHA pin survived and was updated" \
  || bad "git 'revision:' pin was destroyed or not updated — regex is too broad"

grep -qE '^\s*tag:\s+"v0.4.0"' "$WORK/git.rb" && ok "tag: updated" || bad "tag: not updated"
grep -qE '^\s*version "0.4.0"'  "$WORK/git.rb" && ok "version updated" || bad "version not updated"
grep -qE '^\s*license "MIT"'    "$WORK/git.rb" && ok "unrelated lines untouched" || bad "collateral damage to license line"

# ---------------------------------------------------------------------------
# 2. git-checkout with NO packaging revision: must be a clean no-op
# ---------------------------------------------------------------------------
cat > "$WORK/git2.rb" <<'RB'
class NoRev < Formula
  url "https://github.com/happitec-inc/no-rev.git",
      tag:      "v1.0.0",
      revision: "cccccccccccccccccccccccccccccccccccccccc"
  version "1.0.0"
end
RB
python3 "$WORK/git.py" "$WORK/git2.rb" v1.1.0 dddddddddddddddddddddddddddddddddddddddd 1.1.0 1 >/dev/null 2>&1
rc=$?
[ $rc -eq 0 ] && ok "absent packaging revision is not an error" || bad "exited $rc when no revision line present"
grep -qE '^\s*revision: "dddd' "$WORK/git2.rb" && ok "pin still updated with no packaging revision" || bad "pin not updated"

# ---------------------------------------------------------------------------
# 3. asset-download / cask: packaging revision dropped
# ---------------------------------------------------------------------------
cat > "$WORK/cask.rb" <<'RB'
cask "example-app" do
  version "1.29.0"
  revision 2
  sha256 "1111111111111111111111111111111111111111111111111111111111111111"
  url "https://github.com/happitec-inc/example-app-repo/releases/download/v#{version}/ExampleApp.zip"
  name "Agent Dashboard"
end
RB
python3 "$WORK/asset.py" "$WORK/cask.rb" v1.29.0 v1.30.0 \
  2222222222222222222222222222222222222222222222222222222222222222 1.30.0 interpolated 1 >/dev/null 2>&1
rc=$?
[ $rc -eq 0 ] && ok "asset rewriter exits 0" || bad "asset rewriter exited $rc"

grep -qE '^\s*revision 2\s*$' "$WORK/cask.rb" \
  && bad "packaging 'revision 2' still present in cask" \
  || ok "packaging 'revision 2' dropped from cask"

grep -q 'v#{version}' "$WORK/cask.rb" && ok "interpolated url preserved" || bad "interpolation destroyed"
grep -qE '^\s*version "1.30.0"' "$WORK/cask.rb" && ok "cask version updated" || bad "cask version not updated"

# ---------------------------------------------------------------------------
# 4. VERSION UNCHANGED — the packaging revision must be KEPT.
#
# This is the case that makes the drop safe. An asset re-sign republishes the
# SAME tag with a different sha256; the skip guards deliberately fall through
# so the corrected sha lands. Dropping `revision N` on that path takes
# pkg_version 1.29.0_2 -> 1.29.0 — a DOWNGRADE — so `brew upgrade` no-ops and
# the corrected artifact never reaches the machine. That is the exact failure
# `revision` exists to prevent, reintroduced by its own reset.
# ---------------------------------------------------------------------------
cat > "$WORK/resign.rb" <<'RB'
cask "example-app" do
  version "1.29.0"
  revision 2
  sha256 "1111111111111111111111111111111111111111111111111111111111111111"
  url "https://github.com/happitec-inc/example-app-repo/releases/download/v#{version}/ExampleApp.zip"
end
RB
python3 "$WORK/asset.py" "$WORK/resign.rb" v1.29.0 v1.29.0 \
  3333333333333333333333333333333333333333333333333333333333333333 1.29.0 interpolated 0 >/dev/null 2>&1
rc=$?
[ $rc -eq 0 ] && ok "re-sign path exits 0" || bad "re-sign path exited $rc"

grep -qE '^\s*revision 2\s*$' "$WORK/resign.rb" \
  && ok "version unchanged: packaging 'revision 2' PRESERVED" \
  || bad "version unchanged: revision was dropped — this DOWNGRADES pkg_version"

grep -q '3333333333333333333333333333333333333333333333333333333333333333' "$WORK/resign.rb" \
  && ok "re-signed sha256 still written" || bad "sha256 not updated on re-sign"

# git path, same condition
cat > "$WORK/git3.rb" <<'RB'
class Same < Formula
  url "https://github.com/happitec-inc/same.git",
      tag:      "v2.0.0",
      revision: "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
  version "2.0.0"
  revision 3
end
RB
python3 "$WORK/git.py" "$WORK/git3.rb" v2.0.0 ffffffffffffffffffffffffffffffffffffffff 2.0.0 0 >/dev/null 2>&1
grep -qE '^\s*revision 3\s*$' "$WORK/git3.rb" \
  && ok "git path, version unchanged: 'revision 3' PRESERVED" \
  || bad "git path, version unchanged: revision dropped — DOWNGRADE"

# ---------------------------------------------------------------------------
echo
echo "passed: $PASS   failed: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
