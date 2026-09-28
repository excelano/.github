#!/usr/bin/env bash
# Author: David M. Anderson
# Built with AI assistance (Claude, Anthropic)
#
# Submits a release's Windows packages to microsoft/winget-pkgs via komac.
# Ported from ship's internal/release/winget.go + cmd/ship/walk.go "winget"
# target (reference only, carries no authority over this file's own shape).
#
# One repo can carry more than one winget package (xfiles ships five CLIs
# from one repo), so ENTRIES is the whole ship.conf `winget=` line: space-
# separated `PackageID:asset1,asset2,...` groups, `<version>` unsubstituted.
#
# Deliberate divergence from ship: every package always goes through
# `komac new|update --output <dir>` first, is inspected/patched locally,
# and only then `komac submit`. Ship only does the two-step dance when a
# privacy URL needs patching in; here it's universal, because it's what
# makes DRY_RUN a single, uniform gate (skip the submit step) instead of a
# second code path — the idempotency rule this whole pipeline runs on.
#
# Two GitHub tokens, deliberately not one, per ~/notes/build_release_gotchas.md:
# komac needs its own `public_repo`-only PAT (GITHUB_TOKEN below) - a
# fine-grained token can generate manifests but cannot open the PR against
# microsoft/winget-pkgs (komac #310), and the classic public_repo PAT is the
# documented narrow option. `gh repo sync` separately needs `workflow` scope,
# to pull upstream commits touching .github/workflows/ into the fork - a
# scope deliberately kept off komac's own token. GH_TOKEN takes precedence
# over GITHUB_TOKEN for `gh`, so setting both hands each tool the one it
# should have without either seeing the other's.
set -euo pipefail

: "${ENTRIES:?}" "${VERSION:?}" "${SLUG:?}" "${DRY_RUN:=false}" "${GITHUB_TOKEN:?}" "${GH_TOKEN:?}"
PRIVACY_URL="${PRIVACY_URL:-}"

synced=false

# has PATH: whether PATH exists in microsoft/winget-pkgs.
has() {
    gh api "repos/microsoft/winget-pkgs/contents/$1" >/dev/null 2>&1
}

# manifest_path ID: the pool layout - first letter of the publisher, then
# the publisher, then the name.
manifest_path() {
    local id="$1" pub name
    pub="${id%%.*}"
    name="${id#*.}"
    local first
    first=$(printf '%s' "$pub" | cut -c1 | tr '[:upper:]' '[:lower:]')
    printf 'manifests/%s/%s/%s' "$first" "$pub" "$name"
}

# open_pr_for ID: the number of an open PR for this package, if any. Titles
# are all komac gives reliably ("New package: Publisher.Name version X" /
# "Add version: ..."), so match the name as a whole word in the title - the
# same rate rule ship enforces: one open submission per package at a time,
# whatever version it names.
open_pr_for() {
    local id="$1" name pub
    pub="${id%%.*}"
    name="${id#*.}"
    gh search prs --repo microsoft/winget-pkgs --state open "$pub" \
        --json number,title 2>/dev/null |
        jq -r --arg name "$name" \
            '.[] | select(.title | test("(^|[^a-zA-Z0-9])" + $name + "([^a-zA-Z0-9]|$)"; "i")) | .number' |
        head -n1
}

for entry in $ENTRIES; do
    id="${entry%%:*}"
    assets_raw="${entry#*:}"

    echo "== $id =="

    pr=$(open_pr_for "$id" || true)
    if [ -n "$pr" ]; then
        echo "  microsoft/winget-pkgs#$pr is already open for $id; the rate rule is one at a time, skipping"
        continue
    fi

    urls=()
    IFS=',' read -ra assets <<<"$assets_raw"
    for asset in "${assets[@]}"; do
        asset="${asset//<version>/$VERSION}"
        urls+=("--urls" "https://github.com/${SLUG}/releases/download/v${VERSION}/${asset}")
    done

    verb=update
    shipped_before=true
    if ! has "$(manifest_path "$id")"; then
        verb=new
        shipped_before=false
    fi

    out=$(mktemp -d)
    echo "  komac $verb $id --version $VERSION ${urls[*]} --output $out"
    komac "$verb" "$id" --version "$VERSION" "${urls[@]}" --output "$out"

    if [ -n "$PRIVACY_URL" ]; then
        manifest=$(grep -l '^ManifestType: *defaultLocale' "$out"/*.yaml)
        if [ "$(wc -l <<<"$manifest")" != 1 ]; then
            echo "  $id: expected exactly one defaultLocale manifest under $out, found:" >&2
            printf '%s\n' "$manifest" >&2
            exit 1
        fi
        # komac writes CRLF manifests on purpose (~/notes/build_release_gotchas.md);
        # the inserted line matches that convention rather than mixing line
        # endings in the file. komac's `submit` rewrites the manifest in
        # schema order regardless of where the line lands here, so inserting
        # ahead of ManifestType is enough - matches ship's SetPrivacyURL.
        awk -v url="$PRIVACY_URL" \
            '/^ManifestType:/ { printf "PrivacyUrl: %s\r\n", url } { print }' \
            "$manifest" >"$manifest.tmp"
        mv "$manifest.tmp" "$manifest"
        echo "  PrivacyUrl: $PRIVACY_URL"
    fi

    if [ "$DRY_RUN" = "true" ]; then
        echo "  dry run: manifests written to $out, not submitted"
        rm -rf "$out"
        continue
    fi

    if [ "$synced" = "false" ]; then
        echo "  gh repo sync anderix/winget-pkgs"
        gh repo sync anderix/winget-pkgs
        synced=true
    fi

    if [ "$shipped_before" = "false" ]; then
        echo "  first submission for $id; it waits on a moderator, for days or weeks"
    fi

    echo "  komac submit $out --yes"
    komac submit "$out" --yes
    rm -rf "$out"
    echo "  $id $VERSION submitted"
done
