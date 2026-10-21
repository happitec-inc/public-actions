#!/usr/bin/env python3
"""bump-policy — content-validating gate for Homebrew tap bump PRs.

Answers one question: *is this PR exactly a tap bump — one formula or one cask
— and does its payload match what the source repo actually published?*

Identity is not evidence. Every automated author shares one release-bot
identity, so "the bot opened it on a bump/ branch" says nothing about what the
diff does. This driver reads the diff instead.

THREE VERDICTS, not two. The check gates CONTENT; the branch-protection review
count gates NOVELTY. Neither does the other's job:

  PASS     the PR is exactly a bump and its payload verifies against the source
           repo. Green, and the action posts a mechanical approving review so
           `required_approving_review_count: 1` is met without `--admin`.

  ABSTAIN  the PR is not a pure bump — a new formula, a new tap, a download
           strategy change, a branch-pinned formula, extra content riding along.
           Green and SILENT: no approval is emitted, so the missing review still
           blocks the merge and a human or agent reviewer supplies it.

  FAIL     the PR is shaped exactly like a pure bump and its payload assertion
           is FALSE — the tag does not resolve to the pinned revision, the
           release asset is missing or hashes differently, or the version goes
           backwards without the `allow-downgrade` label. Red, hard block.

Abstaining green is safe *because it grants no approval*. There is no path from
"the check had no opinion" to "the check approved": approval is emitted only on
an affirmative attestation that the payload was verified (`approve: true` in the
result JSON), never on the absence of a failure. That matters because a SKIPPED
job reports its check as satisfied to branch protection — once this check is
also an approval source, every skip path would otherwise be an approval path.

Making every non-bump a violation — the two-verdict design this replaced —
inverts the requirement it was supposed to serve: a new-formula PR would fail a REQUIRED
check and become unmergeable, walling off exactly the class of change that is
supposed to get eyes on it.

Three checks, all mechanical:

  1. SHAPE     — the PR touches exactly one file, and it is Formula/*.rb or
                 Casks/*.rb.
  2. LEGALITY  — only bump-legal fields changed. Implemented by MASKING the
                 legal fields in both revisions and requiring the remainder to
                 be byte-identical. A line-diff allowlist can be fooled by a
                 change that happens to look like a legal line; masking cannot,
                 because anything not masked must match exactly.
  3. PAYLOAD   — the new pin is verified against the source repo: the tag must
                 resolve to the pinned revision, or the release asset must
                 hash to the pinned sha256.

Plus a version-monotonicity guard, waivable with the `allow-downgrade` label.

ARTIFACT SHAPES. All four occur in the production tap this gate was built
against; the formula counts are from a full sweep of that tap on 2026-08-07 and
the cask row from the sweep of 2026-09-12. They are recorded because the four
modelled shapes are safe only because someone surveyed them — a fifth shape is
meant to abstain, not to be guessed at:

  git-checkout   (17)  url "...git", tag: "vX", revision: "<sha>"
                       Bump-legal: version, tag:, revision:
  asset-download  (1)  url ".../releases/download/vX/<file>", version, sha256
                       Bump-legal: version, the tag segment of the url, sha256
  branch-pinned   (2)  url "...git", branch: "main", version — no tag/revision
                       ABSTAIN. Tags only. There is no published payload to
                       verify, and the `version` line is decorative — Homebrew
                       builds branch HEAD regardless of what it says. The gate
                       will not auto-approve an edit it cannot check, so it says
                       nothing and lets a reviewer decide.

                       Consequence, stated plainly: a branch-pinned formula
                       cannot AUTO-MERGE through this gate until its source repo
                       cuts real tags and the formula pins them. Such a PR can
                       still be merged by a reviewer. That is a defect in those
                       formulae, not in this check.

  cask            (2)  Casks/*.rb. Homebrew's own directory name, plural, and
                       it is the only thing about a cask this gate had to be
                       taught: a cask bump changes `version` and `sha256` and
                       nothing else, which is the asset-download shape. An
                       earlier revision of this gate rejected a cask purely on
                       its path, and two app releases published without ever
                       reaching brew as a result.

                       One real difference, and it is not cosmetic: a cask
                       INTERPOLATES the version into its url
                       (`/releases/download/v#{version}/...`) where the formula
                       asset-download shape carries a literal tag segment. The
                       segment is therefore the same string on both revisions
                       of a bump, so the literal is useless both for verifying
                       the payload (it 404s) and for the monotonicity guard (it
                       makes it a no-op). See resolve_interpolation().

                       Two limits on what is modelled, both ABSTAIN rather than
                       guess. The resolved segment must LOOK like a version
                       (RE_VERSION_SHAPE), because it lands in a constructed
                       API path as well as in a url. And exactly ONE top-level
                       `url` and one top-level `sha256` are modelled: an
                       arch-conditional cask has two of each, and this gate
                       would pair the first url with the last sha256.

The sha256 subtlety that drove the masking design: `sha256` also appears inside
`resource` blocks in virtualenv formulae, where it pins a *Python dependency*,
not the bump payload. Changing one is a
dependency change and must fail. Only a TOP-LEVEL sha256 is bump-legal, so the
masker tracks `resource ... do`/`end` nesting rather than matching `sha256`
anywhere in the file.

Exit codes:
  0  PASS or ABSTAIN — nothing to block on
  1  usage/config error
  2  FAIL — the PR claims to be a bump and its payload assertion is false
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field

# --- verdicts ----------------------------------------------------------------

PASS = "pass"
ABSTAIN = "abstain"
FAIL = "fail"

# The approving review this gate posts will normally share its App identity
# with whatever other automated reviewer the org runs, so the BODY is the only
# thing that tells a mechanical bump approval apart from a human-equivalent
# content review.
#
# This sentinel is a CONTRACT. Anything matching approvals — a dismissal sweep,
# an audit, a future policy guard — keys on this exact string.
#
# It carries NO version, and both halves of that are load-bearing. A sibling
# policy daemon once matched its own reviews by AUTHOR alone and, in a single
# sweep, dismissed 67 genuine code reviews across 33 PRs in 20 repos; the same
# daemon embedded a version string in its review header, so a routine release
# would have made every previously-posted review permanently unmatchable. Both
# defects are designed out here: a distinct sentinel, and a sentinel that a
# release cannot move.
APPROVAL_SENTINEL = "<!-- bump-policy: mechanical-bump-approval -->"

# --- masking -----------------------------------------------------------------
# Each pattern captures the *value* in group 1 so it can be replaced with a
# placeholder while every other byte on the line is preserved. Whitespace and
# alignment are deliberately NOT normalised: a bump rewrites values in place, so
# re-indenting the url block is not a bump and should fail the gate.

RE_VERSION = re.compile(r'^(\s*version\s+")([^"]*)(")')
RE_TAG = re.compile(r'^(\s*tag:\s*")([^"]*)(")')
RE_REVISION = re.compile(r'^(\s*revision:\s*")([^"]*)(")')
RE_SHA256 = re.compile(r'^(\s*sha256\s+")([^"]*)(")')
RE_URL_TAGSEG = re.compile(r"(/releases/download/)([^/]+)(/)")

# Homebrew's PACKAGING revision — `revision 1`, bare, no colon and no quotes.
# A different thing entirely from the git pin `revision: "<sha>"`, which the
# masker above handles.
#
# A bump generator DELETES this line on a version bump, because the new version
# resets the packaging revision to zero. Masking cannot absorb that: masking replaces a value in place, and a
# removed line has no place left to be. So the generator's own output failed
# this gate on the masked-remainder compare — bump-policy has a 0% lifetime pass
# rate, and two of the five failures were the generator's output.
#
# The fix is DELETION, not masking: the line is removed from both sides before
# they are compared, which is symmetric and preserves the "anything not masked
# must match exactly" guarantee. Adding or changing one is handled separately in
# check() and is NOT absorbed — that is a packaging change and wants a reader.
RE_PACKAGING_REVISION = re.compile(r"^[ \t]*revision[ \t]+\d+[ \t]*$")

# The bump-shaped paths, as ONE constant used by both call sites — the shape
# gate in check() and the blob-reading guard in main(). It was two independent
# literals, and that is how the missing cask support half-existed: a cask bump
# abstained at the shape gate while main() had already declined to read its
# blobs, so widening only one of them would have produced a gate reasoning over
# two empty strings. A single constant makes that class of half-fix impossible.
RE_TAP_ARTIFACT = re.compile(r"(?:Formula|Casks)/[^/]+\.rb")

# `#{...}` Ruby string interpolation. A cask pins its version once and
# interpolates it into the url (`/releases/download/v#{version}/...`), where the
# asset-download FORMULA shape carries a literal tag segment instead. See
# resolve_interpolation().
RE_INTERPOLATION = re.compile(r"#\{([A-Za-z_][A-Za-z0-9_]*)\}")

RE_URL_STATEMENT = re.compile(r'^\s*url\s+"([^"]+)"')

# What a version is allowed to look like once it has been interpolated into a
# url or an API path. `RE_VERSION` captures `[^"]*`, so a version may contain
# `/` and `..`; the literal formula segment never could, because RE_URL_TAGSEG
# captures `[^/]+`, so this became reachable only with interpolation.
#
# Measured on this branch before the guard existed, with `gh` stubbed:
#
#   version "1.47.0/../../../../../evil-owner/evil-repo/releases/tags/v1"
#   -> api repos/<source-owner>/<source-repo>/releases/tags/
#          v1.47.0/../../../../../evil-owner/evil-repo/releases/tags/v1
#
# and that reached `approve: True`. curl normalises dot segments per RFC 3986
# and a constructed API path need not, so for any version carrying `/` or `..`
# the url this gate verifies and the url Homebrew fetches are two different
# URLs — which is exactly "verifying a url the gate did not check". It was
# contained only incidentally: the asset bytes come back by asset id under the
# slug parsed from the first two path segments, so the compare still happened
# against those bytes. Incidental containment is not a guarantee, and this gate
# posts approving reviews that satisfy branch protection.
#
# Validating the shape makes resolve_interpolation()'s claim — that the
# resolved url is the one Homebrew will fetch — true by construction rather
# than by luck. Applied to EVERY asset-download segment, not only interpolated
# ones: `[^/]+` also admits a bare `..`, and a uniform invariant is easier to
# hold than a conditional one. Every asset-download artifact in the tap this
# gate was built against (2 formulae + 2 casks) satisfies it, measured by
# replaying the gate over the tap's real history.
RE_VERSION_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")

RE_RESOURCE_OPEN = re.compile(r'^\s*resource\s+"[^"]*"\s+do\s*$')
RE_BLOCK_OPEN = re.compile(r"\bdo\s*$")
RE_BLOCK_END = re.compile(r"^\s*end\s*$")


def resource_line_flags(lines: list[str]) -> list[bool]:
    """True for each line that sits inside a `resource "..." do ... end` block.

    Tracks nesting depth so a `do` inside a resource (an `install do`, a block
    argument) does not close it early.
    """
    flags: list[bool] = []
    depth = 0  # nesting depth *within* the current resource block
    inside = False
    for line in lines:
        if not inside:
            if RE_RESOURCE_OPEN.match(line):
                inside, depth = True, 1
                flags.append(True)
                continue
            flags.append(False)
            continue

        flags.append(True)
        if RE_BLOCK_END.match(line):
            depth -= 1
            if depth == 0:
                inside = False
        elif RE_BLOCK_OPEN.search(line):
            depth += 1
    return flags


@dataclass
class Payload:
    """The bump-legal field values extracted from one revision of a formula."""

    version: str | None = None
    tag: str | None = None
    revision: str | None = None
    sha256: str | None = None
    url_tag_segment: str | None = None
    url: str | None = None
    branch: str | None = None
    fields_seen: set[str] = field(default_factory=set)

    # The url and its tag segment with `#{version}` substituted, or None when
    # some interpolation in them cannot be resolved from this revision alone.
    # For every formula in the tap these are byte-identical to `url` and
    # `url_tag_segment`; they differ only for an interpolating cask.
    resolved_url: str | None = None
    resolved_url_tag_segment: str | None = None
    # Why resolution of `url` failed, straight from the function that refused
    # it, so check() reports a reason rather than reconstructing one.
    resolve_reason: str | None = None

    # How many top-level `url` statements and `sha256` lines this revision has.
    # The gate models ONE download per artifact: `url` is extracted from the
    # first url statement and `sha256` from the last top-level sha256 line, so
    # on a two-url cask those two come from different downloads. Counting them
    # is what lets check() refuse the shape instead of mis-pairing it.
    url_statements: int = 0
    top_level_sha256s: int = 0


def resolve_interpolation(text: str | None, version: str | None) -> str | None:
    """Substitute `#{version}` in `text`, or return None if it cannot be.

    A cask writes its download url ONCE and interpolates the version into it:

        version "1.47.2-rc.1"
        url ".../releases/download/v#{version}/SomeApp.zip"

    So the literal tag segment in the file is `v#{version}` on every revision —
    it does not move when the cask is bumped. Two things break on that if it is
    taken literally, both measured against the real cask on 2026-09-12:

      * PAYLOAD verification queries `releases/tags/v#{version}` and 404s, so a
        perfectly good cask bump lands as a hard FAIL — strictly worse than the
        path-based ABSTAIN it replaced.
      * The MONOTONICITY guard compares the pin, and the pin came from that
        literal segment, so old == new and the guard becomes a no-op. A cask
        downgrade from 1.46.4 to 1.0.0 passed the guard silently.

    Resolving it is a substitution, not a guess: the version is stated in the
    same revision being checked. Anything OTHER than `version` is not
    resolvable here, and returns None so the caller abstains rather than
    verifying a url it invented.

    THE SHAPE CHECK IS HERE, DELIBERATELY, AND IT IS WHAT MAKES THE RESOLVED
    URL EQUAL THE ONE HOMEBREW FETCHES. A version that is not version-shaped
    (RE_VERSION_SHAPE) is not substituted at all: this returns None and the
    caller abstains. `version` is captured as `[^"]*`, so it may hold `/` and
    `..`; curl normalises dot segments per RFC 3986 and a constructed API path
    need not, so substituting an unchecked version means the gate verifies one
    url while Homebrew fetches another.

    It lives HERE rather than in check() because this is the chokepoint: every
    interpolation — into the tag segment, into an asset name, into a
    subdirectory, anywhere a future caller resolves — passes through this one
    function. That placement is the point, and it is the third position this
    check occupied before it was right:

      round 1  absent. A version of
               `1.47.0/../../../../../evil-owner/evil-repo/releases/tags/v1`
               put the traversal into the release-lookup API path and reached
               `approve: True`.
      round 2  present in check(), behind `interpolated` — a predicate derived
               from the TAG SEGMENT. The comment beside it said the version
               may be interpolated anywhere in the url, and then the code
               asked only about the segment. So
               `/download/v1.46.4/SomeApp-#{version}.zip`, an
               ordinary cask shape with a literal tag, skipped the check and
               reached `approve: True` again — verifying `evil.zip` out of
               `evil-owner/evil-repo` while Homebrew, normalising the dot
               segments, would fetch exactly that.
      round 3  here. No predicate, because the predicate is what was wrong
               twice. If a version reaches a url at all, it came through this
               function.
    """
    return resolve_interpolation_detail(text, version)[0]


def substitute_version(text: str, version: str) -> str:
    r"""Substitute `#{version}` in `text`, literally.

    Its own function so the "literally" half stays independently measurable.
    A LAMBDA replacement, because `sub(version, ...)` treats the replacement as
    a TEMPLATE: a `\1` or a `\g<0>` inside a version string becomes a
    backreference rather than text.

    RE_VERSION_SHAPE now rejects a backslash upstream, so no version this gate
    accepts can reach here carrying a template escape — the lambda is defence
    in depth. That is precisely why this is a separate function: with the
    property only reachable through resolve_interpolation() it became
    unobservable, and a control that cannot observe a property is not a
    control.
    """
    return RE_INTERPOLATION.sub(lambda _m: version, text)


# Why each refusal happened, so check() can say it without re-deriving it. The
# re-derivation is not hypothetical: the round-3 defect was a second place that
# re-asked "is this interpolated?" and got a different answer from the code
# that actually decided.
UNMODELLED_INTERPOLATION = "unmodelled-interpolation"
NOT_JUST_VERSION = "not-just-version"
NO_VERSION = "no-version"
BAD_VERSION_SHAPE = "bad-version-shape"


RESOLVE_REASON_TEXT = {
    UNMODELLED_INTERPOLATION: (
        "it interpolates something this gate does not model — a dotted "
        "receiver such as `#{version.csv.first}`. That is ordinary cask "
        "practice; this gate models a bare `#{version}` and nothing else, so "
        "the shape is unmodelled rather than wrong. A CSV version "
        "(`version \"1.2.3,456\"`) lands here too, and that is the truthful "
        "reason for it — not that the version is malformed."
    ),
    NOT_JUST_VERSION: (
        "it interpolates a name other than `version`. Only `version` is "
        "resolvable, because it is pinned in the same revision under check; "
        "anything else would make the verified url a guess."
    ),
    NO_VERSION: (
        "it interpolates `#{version}` and there is no usable `version` to "
        "substitute."
    ),
    BAD_VERSION_SHAPE: (
        "its version is not shaped like a version, and a version is "
        "interpolated into it. Anything path-structural there makes the url "
        "this gate verifies and the url Homebrew fetches two different URLs — "
        "WHEREVER in the url it appears, an asset name or a subdirectory just "
        "as much as the tag segment. A comma is excluded deliberately and is "
        "reported as unmodelled, not malformed."
    ),
}


def resolve_interpolation_detail(
    text: str | None, version: str | None
) -> tuple[str | None, str | None]:
    """`(resolved, reason)`. `reason` is None exactly when `resolved` is not.

    The order of the refusals is deliberate and is what makes each abstain
    message truthful. An unmodelled interpolation is reported as such even when
    the version ALSO fails the shape check — a CSV version paired with
    `#{version.csv.first}` is ordinary cask practice, and telling that author
    their version is malformed would be a lie. The honest statement is that
    this gate does not model CSV versions.
    """
    if text is None:
        return None, NO_VERSION
    if "#{" not in text:
        # Nothing to resolve. Every formula in the tap takes this branch, which
        # is why the resolved values are byte-identical to the literal ones
        # there.
        return text, None
    # FIRST: is every interpolation one this gate models? A dotted receiver —
    # `#{version.csv.first}`, the common cask idiom — is not matched by
    # RE_INTERPOLATION at all, so counting the openers is what detects it.
    if text.count("#{") != len(RE_INTERPOLATION.findall(text)):
        return None, UNMODELLED_INTERPOLATION
    names = {m.group(1) for m in RE_INTERPOLATION.finditer(text)}
    if names - {"version"}:
        return None, NOT_JUST_VERSION
    if not version:
        # No version to substitute — including the degenerate `version ""`,
        # which would resolve `v#{version}` to a bare `v`.
        return None, NO_VERSION
    if not RE_VERSION_SHAPE.fullmatch(version):
        # Substitution is about to happen and the version is not shaped like a
        # version. Refuse to build a url out of it. THE unbypassable position
        # for this check — see resolve_interpolation()'s docstring.
        return None, BAD_VERSION_SHAPE
    return substitute_version(text, version), None


def mask(text: str) -> tuple[str, Payload]:
    """Return (masked_text, payload).

    Masked text has every bump-legal value replaced by a fixed placeholder, so
    two revisions differing *only* in those values mask to identical strings.
    """
    lines = text.split("\n")
    in_resource = resource_line_flags(lines)
    p = Payload()
    out: list[str] = []

    for line, is_resource in zip(lines, in_resource):
        masked = line

        # Comments are never payload. A real formula in the tap carries a
        # comment block that mentions `/releases/download/...` while explaining
        # its custom download strategy; without this guard a comment can donate
        # the tag segment that gets verified against the source repo. Comments are
        # still compared byte-for-byte in the remainder, so editing one is a
        # violation — they are excluded from EXTRACTION, not from checking.
        is_comment = line.lstrip().startswith("#")

        # A resource block's url/sha256/version pin dependencies, not the bump
        # payload. Leave them untouched so any edit shows up in the remainder.
        if not is_resource and not is_comment:
            m = RE_VERSION.match(masked)
            if m:
                p.version = m.group(2)
                p.fields_seen.add("version")
                masked = f"{m.group(1)}<VERSION>{m.group(3)}{masked[m.end():]}"

            m = RE_TAG.match(masked)
            if m:
                p.tag = m.group(2)
                p.fields_seen.add("tag")
                masked = f"{m.group(1)}<TAG>{m.group(3)}{masked[m.end():]}"

            m = RE_REVISION.match(masked)
            if m:
                p.revision = m.group(2)
                p.fields_seen.add("revision")
                masked = f"{m.group(1)}<REVISION>{m.group(3)}{masked[m.end():]}"

            m = RE_SHA256.match(masked)
            if m:
                p.sha256 = m.group(2)
                p.top_level_sha256s += 1
                p.fields_seen.add("sha256")
                masked = f"{m.group(1)}<SHA256>{m.group(3)}{masked[m.end():]}"

        # The url STATEMENT is the only line that may donate — or mask — a tag
        # segment. It used to be any non-comment line, with the last match
        # winning, which is safe for the tap's formulae (they have exactly one
        # such line) and unsafe for a cask: a real cask in the tap embeds its
        # download-strategy class, whose regex literal
        #
        #     %r{https://github.com/([^/]+)/([^/]+)/releases/download/([^/]+)/(\S+)}
        #
        # matches RE_URL_TAGSEG and donates the segment `([^`. Narrowing to the
        # url statement also stops the masker placeholdering three characters of
        # that regex, which would have carved a small hole in the byte-identical
        # remainder compare.
        if not is_resource and not is_comment:
            mu = RE_URL_STATEMENT.match(line)
            if mu:
                p.url_statements += 1
                if p.url is None:
                    p.url = mu.group(1)
                    m = RE_URL_TAGSEG.search(masked)
                    if m:
                        p.url_tag_segment = m.group(2)
                        p.fields_seen.add("url_tag_segment")
                masked = RE_URL_TAGSEG.sub(r"\1<URLTAG>\3", masked, count=1)

        if p.branch is None and not is_resource and not is_comment:
            # `head "...", branch: "main"` is not the download pin — only a
            # `branch:` on the url statement makes a formula branch-pinned.
            mb = re.search(r'\bbranch:\s*"([^"]+)"', line)
            if mb and not line.lstrip().startswith("head "):
                p.branch = mb.group(1)

        out.append(masked)

    p.resolved_url, url_reason = resolve_interpolation_detail(p.url, p.version)
    p.resolved_url_tag_segment, seg_reason = resolve_interpolation_detail(
        p.url_tag_segment, p.version
    )
    # Whichever actually refused. check() abstains when EITHER is None, so
    # taking the url's reason alone would report "no usable version" for a
    # segment-only refusal. Under correct code the segment is a substring of
    # the url and the two agree; the fallback means a future divergence
    # produces a truthful message instead of a confident wrong one.
    p.resolve_reason = url_reason or seg_reason

    return "\n".join(out), p


def split_packaging_revisions(text: str) -> tuple[str, list[str]]:
    """Return (text with packaging-revision lines removed, those lines).

    Only top-level, non-comment lines count. A `revision 2` inside a `resource`
    block is a dependency's packaging revision, not the formula's, and must stay
    in the compared remainder like every other resource line.
    """
    lines = text.split("\n")
    flags = resource_line_flags(lines)
    kept: list[str] = []
    removed: list[str] = []
    for line, is_resource in zip(lines, flags):
        if (
            not is_resource
            and not line.lstrip().startswith("#")
            and RE_PACKAGING_REVISION.match(line)
        ):
            removed.append(line.strip())
            continue
        kept.append(line)
    return "\n".join(kept), removed


# --- shape -------------------------------------------------------------------

GIT_CHECKOUT = "git-checkout"
ASSET_DOWNLOAD = "asset-download"
BRANCH_PINNED = "branch-pinned"


def classify(p: Payload) -> str:
    if p.tag is not None and p.revision is not None:
        return GIT_CHECKOUT
    if p.url_tag_segment is not None and p.sha256 is not None:
        return ASSET_DOWNLOAD
    if p.branch is not None:
        return BRANCH_PINNED
    return "unknown"


# BRANCH_PINNED is deliberately absent: a branch-pinned formula has no
# verifiable bump. It is recognised only so the abstention can say why — and so
# the row flips on its own the moment that repo cuts real tags (see check()).
LEGAL_FIELDS = {
    GIT_CHECKOUT: {"version", "tag", "revision"},
    ASSET_DOWNLOAD: {"version", "url_tag_segment", "sha256"},
}


# --- version comparison ------------------------------------------------------


def version_key(v: str) -> tuple:
    """Sort key matching `sort -V` closely enough for tap version strings.

    Numeric runs compare numerically, non-numeric runs lexically. Mirrors the
    downgrade guard in the bump generator, which compares with `sort -V`.
    """
    v = v.lstrip("vV")
    parts = re.findall(r"\d+|\D+", v)
    return tuple((0, int(x)) if x.isdigit() else (1, x) for x in parts)


# --- source-repo verification ------------------------------------------------


class VerifyError(Exception):
    pass


def gh(args: list[str], binary: bool = False) -> bytes | str:
    proc = subprocess.run(
        ["gh", *args],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise VerifyError(
            f"gh {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout if binary else proc.stdout.decode("utf-8")


def repo_slug(url: str) -> str:
    m = re.search(r"github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/|$)", url)
    if not m:
        raise VerifyError(f"cannot parse a github repo out of url: {url}")
    return f"{m.group(1)}/{m.group(2)}"


def resolve_tag_commit(slug: str, tag: str) -> str:
    """Return the COMMIT sha that `tag` points at.

    Annotated tags resolve to a tag object, not a commit; bump-tap-formula.yml
    pins the commit, so the tag object must be dereferenced. Getting this wrong
    would fail every annotated-tag bump, which is most of them.
    """
    ref = json.loads(gh(["api", f"repos/{slug}/git/ref/tags/{tag}"]))
    obj = ref["object"]
    if obj["type"] == "commit":
        return obj["sha"]
    if obj["type"] == "tag":
        tag_obj = json.loads(gh(["api", f"repos/{slug}/git/tags/{obj['sha']}"]))
        return tag_obj["object"]["sha"]
    raise VerifyError(f"tag {tag} resolves to unexpected object type {obj['type']}")


def verify_git_checkout(p: Payload, log) -> dict:
    slug = repo_slug(p.url or "")
    commit = resolve_tag_commit(slug, p.tag)
    log(f"source {slug} tag {p.tag} -> {commit}")
    if commit.lower() != (p.revision or "").lower():
        raise VerifyError(
            f"revision does not match the source tag.\n"
            f"    formula pins revision: {p.revision}\n"
            f"    {slug} tag {p.tag} is:  {commit}"
        )
    log(f"revision matches the published tag ({commit[:12]})")
    return {
        "source_repo": slug,
        "evidence": f"tag `{p.tag}` on `{slug}` resolves to commit `{commit}`, "
        f"which is exactly the `revision:` this PR pins",
    }


def verify_asset_download(p: Payload, log) -> dict:
    # The RESOLVED url throughout: a cask's `#{version}` has to be substituted
    # before any of this means anything, and check() has already abstained on
    # anything it could not resolve, so resolved_* is non-None here. For a
    # formula the resolved url is byte-identical to the literal one.
    url = p.resolved_url or p.url or ""
    slug = repo_slug(url)
    tag = p.resolved_url_tag_segment or p.url_tag_segment
    asset_name = url.rstrip("/").split("/")[-1]
    rel = json.loads(gh(["api", f"repos/{slug}/releases/tags/{tag}"]))
    assets = {a["name"]: a for a in rel.get("assets", [])}
    if asset_name not in assets:
        raise VerifyError(
            f"release {tag} on {slug} has no asset named {asset_name} "
            f"(has: {', '.join(sorted(assets)) or 'none'})"
        )
    asset_id = assets[asset_name]["id"]
    log(f"downloading {slug} {tag} asset {asset_name} to hash it")
    blob = gh(
        [
            "api",
            f"repos/{slug}/releases/assets/{asset_id}",
            "-H",
            "Accept: application/octet-stream",
        ],
        binary=True,
    )
    digest = hashlib.sha256(blob).hexdigest()
    if digest.lower() != (p.sha256 or "").lower():
        raise VerifyError(
            f"sha256 does not match the published asset.\n"
            f"    formula pins: {p.sha256}\n"
            f"    asset hashes: {digest}"
        )
    log(f"sha256 matches the published asset ({digest[:12]}, {len(blob)} bytes)")
    return {
        "source_repo": slug,
        "evidence": f"release `{tag}` on `{slug}` ships `{asset_name}` "
        f"({len(blob)} bytes), and it hashes to the `sha256` this PR pins "
        f"(`{digest}`)",
    }


# --- main --------------------------------------------------------------------


@dataclass
class Result:
    """The gate's verdict plus the facts an approval body needs to cite."""

    verdict: str
    reasons: list[str] = field(default_factory=list)
    path: str | None = None
    shape: str | None = None
    old_pin: str | None = None
    new_pin: str | None = None
    source_repo: str | None = None
    evidence: str | None = None
    verified: bool = False

    @property
    def approve(self) -> bool:
        """True only on an AFFIRMATIVE attestation that the payload verified.

        Never derived from the absence of a failure. `--no-verify` reaches
        PASS for the unit suite's benefit but must never carry an approval.
        """
        return self.verdict == PASS and self.verified


def check(
    old_text: str,
    new_text: str,
    changed_files: list[str],
    allow_downgrade: bool,
    verify: bool,
    log,
    base_exists: bool = True,
) -> Result:
    """Classify the PR as PASS, ABSTAIN or FAIL.

    `base_exists=False` means the changed file is new on this PR — a new
    formula or cask, or the first artifact of a new tap. That is the case a
    maintainer most wants a reader on, so it ABSTAINS: green, no approval,
    blocked by the missing review rather than by a red required check.
    """

    def abstain(reason: str, **kw) -> Result:
        return Result(ABSTAIN, [reason], **kw)

    # 1. SHAPE — is this even bump-shaped? Anything that is not, abstains. ----
    if len(changed_files) != 1:
        return abstain(
            f"not a pure bump: a bump PR touches exactly one file; this PR touches "
            f"{len(changed_files)}: {', '.join(changed_files) or '(none)'}"
        )
    path = changed_files[0]
    if not RE_TAP_ARTIFACT.fullmatch(path):
        return abstain(
            f"not a pure bump: a bump PR touches a Formula/*.rb or Casks/*.rb "
            f"file; this PR touches {path}"
        )
    if not base_exists:
        return abstain(
            f"not a pure bump: {path} does not exist on the base branch. Adding a "
            "formula or a cask (or standing up a new tap) is new content, not a "
            "bump — it needs a reviewer, human or agent.",
            path=path,
        )
    log(f"single tap artifact changed: {path}")

    old_masked, old_p = mask(old_text)
    new_masked, new_p = mask(new_text)

    shape = classify(old_p)
    if shape == "unknown":
        return abstain(
            "not a pure bump: cannot classify this formula (no tag:/revision:, no "
            "release-download url + sha256, no branch:) — refusing to guess at what "
            "a legal bump is",
            path=path,
        )
    if classify(new_p) != shape:
        return abstain(
            f"not a pure bump: this PR changes the formula's download strategy "
            f"({shape} -> {classify(new_p)})",
            path=path,
            shape=shape,
        )
    if shape == BRANCH_PINNED:
        # Tags only. A branch-pinned formula tracks a moving branch, so its
        # `version` line is decorative — Homebrew builds branch HEAD whatever it
        # says — and there is no published payload to verify a pin against.
        # There is nothing here this gate can attest to, so it attests to
        # nothing and leaves the PR to a reviewer.
        return abstain(
            f"not a pure bump: {path} is branch-pinned (branch: {old_p.branch!r}) "
            "and has no verifiable pin, so this gate has no opinion on it.\n"
            "    Its `version` line is decorative: Homebrew builds branch HEAD "
            "regardless of what it says, so a version-only edit asserts something "
            "nothing checks.\n"
            "    To make it auto-approvable, fix the formula, not the PR: have the "
            "source repo cut real tags and pin `tag:` + `revision:` (or a release "
            "asset + sha256).",
            path=path,
            shape=shape,
        )
    log(f"shape: {shape}")

    # ONE DOWNLOAD PER ARTIFACT. `mask()` extracts `url` from the FIRST url
    # statement and `sha256` from the LAST top-level sha256 line, and it masks
    # the tag segment on every url statement. On an arch-conditional cask
    # (`on_arm` / `on_intel`, ordinary cask practice) that produces two
    # failures, both measured on this branch:
    #
    #   * a bump to 2.0.0 that ALSO repoints the second url to v9.9.9 returns
    #     PASS with no reasons — the retarget masks to <URLTAG> on both sides
    #     and vanishes from the byte-identical remainder compare. Same class of
    #     hole as the strategy regex this PR closes, one line further along.
    #   * a LEGITIMATE multi-arch bump pairs the arm url with the intel sha256
    #     and hard-FAILs, which is the outcome this gate exists to avoid.
    #
    # No cask in the tap this gate was built against has this shape today. The
    # four modelled shapes are safe because someone surveyed them; and
    # `Casks/*.rb` admits every cask, so
    # the shape has to be refused rather than assumed absent. "This cask has
    # two download urls and this gate models one" is a fine thing to tell a
    # reader.
    urls = max(old_p.url_statements, new_p.url_statements)
    shas = max(old_p.top_level_sha256s, new_p.top_level_sha256s)
    if urls > 1 or shas > 1:
        return abstain(
            f"not a pure bump: {path} declares {urls} top-level `url` "
            f"statement(s) and {shas} top-level `sha256` line(s). This gate "
            "models exactly one download per artifact — an arch-conditional or "
            "otherwise multi-url cask would have its first url checked against "
            "its last sha256, so it gets no opinion and no approval here.",
            path=path,
            shape=shape,
        )

    if shape == ASSET_DOWNLOAD:
        # An interpolating url (a cask) is checkable, but only once `#{version}`
        # is resolved from the same revision. Everything resolve_interpolation()
        # refuses lands here, and there is NO PREDICATE in front of it: if the
        # url interpolates anything at all, its resolution went through that
        # function and either produced a url or produced None.
        #
        # The REASON comes from the function that refused, never re-derived
        # here. Re-deriving is not a hypothetical hazard: the round-3 defect was
        # a second place asking "is this interpolated?" that got a different
        # answer from the code which actually decided.
        if new_p.resolved_url is None or new_p.resolved_url_tag_segment is None:
            return abstain(
                f"not a pure bump: {path} has a url this gate will not resolve "
                f"({new_p.url!r}): {RESOLVE_REASON_TEXT[new_p.resolve_reason or NO_VERSION]}\n"
                "    No opinion, so no approval — a reviewer decides.",
                path=path,
                shape=shape,
            )
        interpolated = "#{" in (old_p.url_tag_segment or "") or "#{" in (
            new_p.url_tag_segment or ""
        )
        if interpolated and old_p.url_tag_segment != new_p.url_tag_segment:
            return abstain(
                f"not a pure bump: this PR rewrites the url's tag TEMPLATE "
                f"({old_p.url_tag_segment!r} -> {new_p.url_tag_segment!r}). "
                "Changing how the tag is derived is a download change, not a "
                "bump, and wants a reader.",
                path=path,
                shape=shape,
            )
        # The resolved segment goes into a url AND into a constructed API path
        # (`repos/<slug>/releases/tags/<segment>`), so its shape is
        # load-bearing rather than cosmetic. See RE_VERSION_SHAPE.
        # `or ""` so this cannot TypeError if the guard above is ever removed or
        # reordered: an unresolvable segment is None, and a crash in a gate is a
        # worse failure than an abstain. Found by the negative control: the
        # mutation that removed the guard turned the suite red by ABORTING it,
        # which also meant every test after the crash went unmeasured.
        if not RE_VERSION_SHAPE.fullmatch(new_p.resolved_url_tag_segment or ""):
            return abstain(
                f"not a pure bump: the url's tag segment resolves to "
                f"{new_p.resolved_url_tag_segment!r}, which is not shaped like a "
                "version. It is interpolated into both the download url and the "
                "release-lookup API path, so a `/` or a `..` in it would mean "
                "this gate verifies one url while Homebrew fetches another. No "
                "opinion, so no approval — a reviewer decides.",
                path=path,
                shape=shape,
            )
        # NO version check here. It used to sit behind `interpolated`, and that
        # predicate — derived from the tag SEGMENT — is exactly the defect of
        # review round 3: a url interpolating the version into its asset name
        # with a literal tag has no `#{` in its segment, so the check was
        # skipped and `approve: True` was reachable on a two-line diff whose
        # download resolved to another repository. The check now lives in
        # resolve_interpolation(), which every interpolation must pass through,
        # so there is no predicate to be wrong about. `interpolated` survives
        # only for the template-rewrite guard above, which is genuinely about
        # the segment, and for this log line.
        if interpolated:
            log(
                f"url tag segment {new_p.url_tag_segment!r} resolves to "
                f"{new_p.resolved_url_tag_segment!r}"
            )

    # 2. LEGALITY ------------------------------------------------------------
    # Packaging-revision lines come out of BOTH sides before the compare, so a
    # generator-authored DELETION is absorbed. Asymmetric cases (an addition, a
    # changed value) are caught here instead — deleting them from both sides
    # would otherwise make an addition invisible.
    old_cmp, old_pkg_rev = split_packaging_revisions(old_masked)
    new_cmp, new_pkg_rev = split_packaging_revisions(new_masked)

    if len(new_pkg_rev) > len(old_pkg_rev):
        return abstain(
            f"not a pure bump: this PR ADDS a packaging `revision` line "
            f"({', '.join(new_pkg_rev)}). Re-packaging the same upstream version "
            "is a build change, not a bump, and wants a reader.",
            path=path,
            shape=shape,
        )
    if len(new_pkg_rev) == len(old_pkg_rev) and new_pkg_rev != old_pkg_rev:
        return abstain(
            f"not a pure bump: this PR changes the packaging `revision` "
            f"({', '.join(old_pkg_rev)} -> {', '.join(new_pkg_rev)}). Re-packaging "
            "is a build change, not a bump, and wants a reader.",
            path=path,
            shape=shape,
        )
    if len(new_pkg_rev) < len(old_pkg_rev):
        # The bump generator drops it on a version bump: the new version
        # resets the packaging revision.
        log(
            f"packaging `revision` dropped by the bump "
            f"({', '.join(old_pkg_rev)}) — expected on a version bump"
        )

    if old_cmp != new_cmp:
        import difflib

        diff = [
            l
            for l in difflib.unified_diff(
                old_cmp.split("\n"),
                new_cmp.split("\n"),
                fromfile="base (masked)",
                tofile="head (masked)",
                lineterm="",
                n=1,
            )
        ]
        # The smuggled-change case: bump-legal fields moved AND something else
        # came along. ABSTAIN, not FAIL — the PR is not lying about its payload,
        # it is a larger change than a bump, and a larger change needs a reader.
        return abstain(
            "not a pure bump: the PR changes more than the bump-legal fields. With "
            "every legal field masked out, the two revisions still differ:\n"
            + "\n".join(f"    {l}" for l in diff[:40]),
            path=path,
            shape=shape,
        )

    legal = LEGAL_FIELDS[shape]
    changed_fields = {
        f
        for f in ("version", "tag", "revision", "sha256", "url_tag_segment")
        if getattr(old_p, f) != getattr(new_p, f)
    }
    illegal = changed_fields - legal
    if illegal:
        return abstain(
            f"not a pure bump: changed field(s) {sorted(illegal)} are not bump-legal "
            f"for a {shape} formula (legal: {sorted(legal)})",
            path=path,
            shape=shape,
        )
    if not changed_fields:
        # Nothing bump-legal moved and the remainder is identical, so there is
        # no payload assertion to be false about. Not a bump; not a lie.
        return abstain(
            "not a pure bump: no bump-legal field changed — this PR does not bump "
            "anything",
            path=path,
            shape=shape,
        )
    log(f"changed fields: {sorted(changed_fields)}")

    # From here on the PR IS shaped exactly like a pure bump. Everything below
    # is the payload assertion, and a false assertion is a hard FAIL.
    # RESOLVED, not literal. A cask's literal segment is `v#{version}` on both
    # revisions, so pinning on it makes old_pin == new_pin and the monotonicity
    # guard below a silent no-op — measured: a 1.46.4 -> 1.0.0 cask downgrade
    # passed. Resolving restores the comparison to v1.46.4 -> v1.0.0. For every
    # formula the resolved value is the literal one, so nothing moves there.
    old_pin = old_p.tag or old_p.resolved_url_tag_segment or old_p.version
    new_pin = new_p.tag or new_p.resolved_url_tag_segment or new_p.version
    r = Result(
        PASS, [], path=path, shape=shape, old_pin=old_pin, new_pin=new_pin
    )

    # 3. MONOTONICITY --------------------------------------------------------
    # EVERY candidate pin that moved, not just the reported one. Five formulae
    # carry no version line (Homebrew infers it), so guarding on `version` alone
    # would silently skip them — an earlier, real bug — but guarding on a
    # single precedence-chosen pin has a mirror-image blind spot, measured in
    # review round 3:
    #
    #   url ".../releases/download/v1.46.4/SomeApp-#{version}.zip"
    #
    # is an ordinary cask shape whose TAG SEGMENT is static. The precedence
    # `tag or segment or version` picked that static segment, so old_pin ==
    # new_pin and the guard was the same silent no-op this PR fixed for the
    # interpolated-segment shape. A 1.46.4 -> 1.0.0 downgrade passed.
    #
    # Checking every candidate that actually changed has no such blind spot and
    # needs no precedence rule to be right. A candidate that did not move
    # cannot mask one that did, and a regression in ANY of them is a
    # regression. `old_pin`/`new_pin` stay the reported pair so the approval
    # body and result JSON are unchanged.
    candidates = [
        ("tag", old_p.tag, new_p.tag),
        ("url tag segment", old_p.resolved_url_tag_segment, new_p.resolved_url_tag_segment),
        ("version", old_p.version, new_p.version),
    ]
    moved = [(n, o, w) for n, o, w in candidates if o and w and o != w]
    regressed = [
        (n, o, w) for n, o, w in moved if version_key(w) < version_key(o)
    ]
    if regressed:
        if allow_downgrade:
            for n, o, w in regressed:
                log(
                    f"::warning::DOWNGRADE (deliberate): {n} {o} -> {w}, "
                    "permitted by the allow-downgrade label"
                )
        else:
            r.verdict = FAIL
            for n, o, w in regressed:
                r.reasons.append(
                    f"version goes backwards: {o} -> {w} (the {n}). A downgrade "
                    "is normally a mis-cut tag. Add the `allow-downgrade` label "
                    "to the PR if this rollback is deliberate."
                )
            return r
    if moved:
        log("; ".join(f"{n}: {o} -> {w}" for n, o, w in moved))
    elif old_pin and new_pin:
        log(f"version: {old_pin} -> {new_pin}")

    # 4. PAYLOAD -------------------------------------------------------------
    if not verify:
        log("::notice::payload verification skipped (--no-verify)")
        return r

    try:
        if shape == GIT_CHECKOUT:
            facts = verify_git_checkout(new_p, log)
        else:
            facts = verify_asset_download(new_p, log)
    except VerifyError as e:
        r.verdict = FAIL
        r.reasons.append(str(e))
        return r

    r.source_repo = facts["source_repo"]
    r.evidence = facts["evidence"]
    r.verified = True
    return r


def result_payload(r: Result) -> dict:
    """The result JSON the composite action reads.

    Factored out of main() so `approve` can be asserted directly. It is the
    field the approving-review step is gated on, and it must be the Result's own
    attestation — never re-derived here from "the verdict is not FAIL", which is
    what a green EXIT means and which would hand an approval to every ABSTAIN.
    """
    return {
        "verdict": r.verdict,
        "approve": r.approve,
        "verified": r.verified,
        "path": r.path,
        "shape": r.shape,
        "old_pin": r.old_pin,
        "new_pin": r.new_pin,
        "source_repo": r.source_repo,
        "reasons": r.reasons,
        "approval_body": approval_body(r) if r.approve else "",
    }


def approval_body(r: Result) -> str:
    """The review body posted on PASS.

    Opens with APPROVAL_SENTINEL and says plainly what it is and is not, so a
    reader (or a matcher) never mistakes it for someone having read the diff.
    """
    return "\n".join(
        [
            APPROVAL_SENTINEL,
            "",
            "**Mechanical bump approval — this is not a content review.**",
            "",
            "`bump-policy` verified that this PR is exactly a tap bump — one "
            "formula or one cask — and that its payload matches what the source "
            "repo published. Nobody has read this diff for intent, and this "
            "approval asserts nothing about whether the bump is a good idea.",
            "",
            f"- artifact: `{r.path}`",
            f"- shape: `{r.shape}`",
            f"- pin: `{r.old_pin}` → `{r.new_pin}`",
            f"- payload: {r.evidence}",
            "- nothing outside the bump-legal fields changed: with every legal "
            "field masked out, base and head are byte-identical",
            "",
            "Novelty still needs eyes. This approval is emitted only for a pure, "
            "payload-verified bump; a new formula or cask, a new tap, a "
            "download-strategy change or any extra content gets no approval "
            "from this check and still needs a human or agent reviewer.",
        ]
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-dir", required=True, help="checkout of the tap repo")
    ap.add_argument("--base-sha", required=True)
    ap.add_argument("--head-sha", required=True)
    ap.add_argument(
        "--allow-downgrade",
        action="store_true",
        help="permit a backwards version bump (set from the PR label)",
    )
    ap.add_argument(
        "--no-verify",
        action="store_true",
        help="skip source-repo payload verification (unit tests only). Never "
        "reaches approve=true — an approval requires verification to have run.",
    )
    ap.add_argument(
        "--result-json",
        help="write the verdict, the reasons and the approval body to this path. "
        "The composite action reads it to decide whether to post an approval.",
    )
    args = ap.parse_args()

    def log(msg: str) -> None:
        print(msg, flush=True)

    def git(*a: str) -> str:
        proc = subprocess.run(
            ["git", "-C", args.repo_dir, *a], capture_output=True, check=False
        )
        if proc.returncode != 0:
            print(
                f"git {' '.join(a)} failed: {proc.stderr.decode('utf-8', 'replace')}",
                file=sys.stderr,
            )
            sys.exit(1)
        return proc.stdout.decode("utf-8")

    def blob_exists(sha: str, path: str) -> bool:
        proc = subprocess.run(
            ["git", "-C", args.repo_dir, "cat-file", "-e", f"{sha}:{path}"],
            capture_output=True,
            check=False,
        )
        return proc.returncode == 0

    changed = [f for f in git("diff", "--name-only", args.base_sha, args.head_sha).split("\n") if f]

    # Read both revisions of the single changed file. A brand-new formula or
    # cask has no base revision; that is an addition, not a bump, and it
    # ABSTAINS — it is precisely the class of change that needs eyes, so the
    # gate must not hard block it, and must not approve it either.
    old_text = new_text = ""
    base_exists = True
    if len(changed) == 1 and RE_TAP_ARTIFACT.fullmatch(changed[0]):
        base_exists = blob_exists(args.base_sha, changed[0])
        if base_exists:
            old_text = git("show", f"{args.base_sha}:{changed[0]}")
        if blob_exists(args.head_sha, changed[0]):
            new_text = git("show", f"{args.head_sha}:{changed[0]}")

    result = check(
        old_text,
        new_text,
        changed,
        allow_downgrade=args.allow_downgrade,
        verify=not args.no_verify,
        log=log,
        base_exists=base_exists,
    )

    if args.result_json:
        # The action gates the approving review on the `approve` field here, not
        # on this process's exit code: a green exit covers both PASS and ABSTAIN
        # and only one of them may approve.
        with open(args.result_json, "w", encoding="utf-8") as fh:
            json.dump(result_payload(result), fh, indent=2)

    if result.verdict == FAIL:
        print("\n::error::bump-policy: FAIL — this PR is shaped like a bump, but its payload does not verify")
        for v in result.reasons:
            print(f"\n  - {v}")
        return 2

    if result.verdict == ABSTAIN:
        # Green and silent by design. No approval is emitted, so the branch's
        # review requirement still blocks the merge until someone reads it.
        print("\nbump-policy: ABSTAIN — no opinion, and no approval emitted.")
        for v in result.reasons:
            print(f"\n  - {v}")
        print(
            "\nThis check does not block. The PR still needs a review from a human "
            "or an agent reviewer before it can merge."
        )
        return 0

    tail = (
        "bump-legal fields only"
        if args.no_verify
        else "bump-legal fields only, payload verified"
    )
    print(f"\nbump-policy: PASS — single tap artifact, {tail}")
    if not result.approve:
        print("::notice::no approval emitted (payload verification did not run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
