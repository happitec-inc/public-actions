#!/usr/bin/env bash
# Tests for bump-tap-formula's asset-download url-style handling.
#
# This script EXTRACTS both:
#   1. The shell's URL_STYLE classification logic
#   2. The asset-mode python rewriter
# straight out of .github/workflows/bump-tap-formula.yml and executes them.
#
# If either the classification logic (grep -> URL_STYLE) or the rewriter's
# interpolation preservation is broken, this suite fails loudly.
#
# Usage: bash tests/bump-tap-url-style.sh
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

WF="${WF:-.github/workflows/bump-tap-formula.yml}"
[ -f "$WF" ] || { echo "cannot find $WF"; exit 1; }

WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT

# 1. Pull the URL_STYLE classification snippet out of the workflow.
python3 - "$WF" "$WORK/detect_style.sh" <<'PY'
import re, sys
wf, out = sys.argv[1], sys.argv[2]
src = open(wf, encoding="utf-8").read()
match = re.search(r'(if grep -qF \'/releases/download/v#\{version\}/\' "\$FORMULA_PATH"; then.*?fi)', src, re.DOTALL)
if not match:
    sys.stderr.write("Failed to extract URL_STYLE classification from workflow\n")
    sys.exit(2)
open(out, "w", encoding="utf-8").write(match.group(1))
PY
[ -s "$WORK/detect_style.sh" ] || { echo "style extraction produced nothing"; exit 1; }
DETECT_STYLE_CMD=$(cat "$WORK/detect_style.sh")

detect_style() {
  local f="$1"
  FORMULA_PATH="$f" bash -c "$DETECT_STYLE_CMD; printf '%s' \"\$URL_STYLE\""
}

# 2. Pull the asset-mode heredoc (the 2nd of the two) out of the workflow.
python3 - "$WF" "$WORK/rewriter.py" <<'PY'
import re, sys, textwrap
wf, out = sys.argv[1], sys.argv[2]
src = open(wf, encoding="utf-8").read()
blocks = re.findall(r"<<'PY'\n(.*?)\n          PY\n", src, flags=re.S)
if len(blocks) != 2:
    sys.stderr.write(f"expected exactly 2 embedded PY heredocs in {wf}, found {len(blocks)}.\n"
                     "The extractor is now testing the wrong thing — fix it before trusting this suite.\n")
    sys.exit(2)
open(out, "w", encoding="utf-8").write(textwrap.dedent(blocks[1]))
PY
[ -s "$WORK/rewriter.py" ] || { echo "rewriter extraction produced nothing"; exit 1; }

INTERPOLATED='cask "demo" do
  version "1.28.1"
  sha256 "'$(printf 'a%.0s' $(seq 64))'"

  url "https://github.com/example-org/demo/releases/download/v#{version}/Demo.zip"
end'

LITERAL='class Demo < Formula
  url "https://github.com/example-org/demo/releases/download/v0.1.11/demo.tar.gz"
  version "0.1.11"
  sha256 "'$(printf 'a%.0s' $(seq 64))'"
end'

NEWSHA=$(printf 'b%.0s' $(seq 64))
pass=0; fail=0

echo "== URL_STYLE classification detection (from workflow) =="
check_style() { # <desc> <content> <expected_style>
  local desc="$1" content="$2" want="$3"
  local f="$WORK/fixture_style.rb"
  printf '%s\n' "$content" > "$f"
  local got
  got=$(detect_style "$f")
  if [ "$got" = "$want" ]; then
    pass=$((pass+1)); printf '  ok   %s\n' "$desc"
  else
    fail=$((fail+1)); printf '  FAIL %-56s expected=%s got=%s\n' "$desc" "$want" "$got"
  fi
}
check_style "interpolated URL detected as interpolated" "$INTERPOLATED" "interpolated"
check_style "literal URL detected as literal"           "$LITERAL"      "literal"

run_case() { # <desc> <content> <old_tag> <new_tag> <new_sha> <new_ver> <expect_rc> <assert_fn>
  local desc="$1" content="$2" old_tag="$3" new_tag="$4" new_sha="$5" new_ver="$6" expect_rc="$7" assert_fn="$8"
  local f="$WORK/fixture.rb"
  printf '%s\n' "$content" > "$f"
  # Style is derived directly from the fixture using the workflow's extracted detection logic!
  local style
  style=$(detect_style "$f")
  python3 "$WORK/rewriter.py" "$f" "$old_tag" "$new_tag" "$new_sha" "$new_ver" "$style" 1 >"$WORK/out" 2>"$WORK/err"
  local rc=$?
  if [ "$rc" -ne "$expect_rc" ]; then
    fail=$((fail+1)); printf '  FAIL %-56s rc=%s expected=%s  %s\n' "$desc" "$rc" "$expect_rc" "$(head -1 "$WORK/err")"
    return
  fi
  if "$assert_fn" "$f"; then
    pass=$((pass+1)); printf '  ok   %s\n' "$desc"
  else
    fail=$((fail+1)); printf '  FAIL %-56s (assertion)\n' "$desc"
    grep -nE 'url|version |sha256' "$f" | sed 's/^/        | /'
  fi
}

# These are invoked indirectly, by name, through run_case's $assert_fn.
# shellcheck disable=SC2329
url_keeps_interpolation() { grep -qF '/releases/download/v#{version}/' "$1"; }
version_bumped()          { grep -qF 'version "1.29.0"' "$1"; }
sha_replaced()            { grep -qF "sha256 \"$NEWSHA\"" "$1"; }
url_tag_rewritten()       { grep -qF '/releases/download/v0.1.12/' "$1" && ! grep -qF 'v0.1.11' "$1"; }
always_true()             { return 0; }

echo "== interpolated url (casks with #{version}) — url must survive untouched =="
run_case "url keeps #{version} interpolation" "$INTERPOLATED" "v1.28.1" "v1.29.0" "$NEWSHA" "1.29.0" 0 url_keeps_interpolation
run_case "version line is bumped"             "$INTERPOLATED" "v1.28.1" "v1.29.0" "$NEWSHA" "1.29.0" 0 version_bumped
run_case "sha256 is replaced"                 "$INTERPOLATED" "v1.28.1" "v1.29.0" "$NEWSHA" "1.29.0" 0 sha_replaced

echo "== literal url — rewritten with new release tag =="
run_case "literal tag segment IS rewritten"   "$LITERAL" "v0.1.11" "v0.1.12" "$NEWSHA" "0.1.12" 0 url_tag_rewritten

echo "== guards =="
# An interpolated url with no version line has nothing to interpolate.
run_case "interpolated without version line fails" \
  "$(printf '%s\n' "$INTERPOLATED" | grep -v 'version "1.28.1"')" \
  "v1.28.1" "v1.29.0" "$NEWSHA" "1.29.0" 2 always_true

echo "== shell parse layer =="
# Pull the parse pipelines out of the workflow
PARSE=$(grep -E "^\s+(OLD_TAG|ASSET|SRC_REPO)=\\\$\(grep " "$WF" | sed 's/^[[:space:]]*//')
N_PARSE=$(printf '%s\n' "$PARSE" | wc -l | tr -d ' ')
if [ "$N_PARSE" -ne 4 ]; then
  echo "  FAIL extracted $N_PARSE parse pipelines from $WF, expected 4."
  exit 1
fi

WITH_CLASS="$WORK/with-class.rb"
cat > "$WITH_CLASS" <<'RB'
class ExampleStrategy < CurlDownloadStrategy
  def parse_url_pattern
    url_pattern = %r{https://github.com/([^/]+)/([^/]+)/releases/download/([^/]+)/(\S+)}
  end
end

cask "demo" do
  version "1.28.1"
  sha256 "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

  url "https://github.com/example-org/demo-app/releases/download/v#{version}/DemoApp.zip"
end
RB

parse_check() { # <desc> <var> <expected>
  local desc="$1" var="$2" want="$3" got
  got=$(FORMULA_PATH="$WITH_CLASS" bash -c "$PARSE; printf '%s' \"\$$var\"" 2>/dev/null)
  if [ "$got" = "$want" ]; then
    pass=$((pass+1)); printf '  ok   %s\n' "$desc"
  else
    fail=$((fail+1)); printf '  FAIL %-56s expected=%s got=%s\n' "$desc" "$want" "$got"
  fi
}

parse_check "ASSET skips the class regex line"    ASSET    "DemoApp.zip"
parse_check "SRC_REPO resolves the real repo"     SRC_REPO "example-org/demo-app"
parse_check "literal parse yields raw interpolation" OLD_TAG "v#{version}"

# Test version interpolation in the asset filename itself (e.g. displayctrl-v#{version}-macos.zip)
DISPLAYCTRL="$WORK/displayctrl.rb"
cat > "$DISPLAYCTRL" <<'RB'
cask "displayctrl" do
  version "0.2.1"
  sha256 "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

  url "https://github.com/happitec-inc/displayctrl/releases/download/v#{version}/displayctrl-v#{version}-macos.zip"
end
RB
dc_asset=$(FORMULA_PATH="$DISPLAYCTRL" bash -c "$PARSE; ASSET=\"\${ASSET//\\#\\{version\\}/0.2.2}\"; printf '%s' \"\$ASSET\"" 2>/dev/null)
if [ "$dc_asset" = "displayctrl-v0.2.2-macos.zip" ]; then
  pass=$((pass+1)); printf '  ok   %s\n' "version-interpolated asset filename evaluates correctly"
else
  fail=$((fail+1)); printf '  FAIL %-56s expected=%s got=%s\n' "version-interpolated asset filename evaluates correctly" "displayctrl-v0.2.2-macos.zip" "$dc_asset"
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "PASS — $pass/$((pass+fail))"
else
  echo "FAIL — $fail of $((pass+fail)) failed"
fi
[ "$fail" -eq 0 ]
