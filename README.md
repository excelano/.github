# excelano/.github

Org-wide defaults for the excelano repos. Right now that means reusable CI
workflows: the policy lives here once, and each repo calls it instead of
carrying its own copy.

## Calling a workflow

A consumer repo's `.github/workflows/ci.yml` is a few lines:

```yaml
name: ci

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  ci:
    uses: excelano/.github/.github/workflows/go-ci.yml@main
```

The trigger stays in the caller, because when a repo builds is the caller's
business; what a build checks is the fleet's. Pass `with:` inputs for the few
things a repo may legitimately differ on — the Go workflow accepts
`go-version`, which defaults to stable.

Pinning `@main` rather than a tag is deliberate. A correction to the policy
should reach every repo the next time it builds, which is the reason the file
is here and not copied into each one.

## Why one host per org

The anderix repos call `anderix/.github` instead of this repo, even for an
identical workflow. Tools under anderix may not depend on excelano; two copies
of a policy across two orgs is a boundary worth keeping, and it is not the
nineteen copies this repo exists to prevent.

This repo must stay public. A private one cannot serve reusable workflows to
public callers.

## What does not belong here

GitHub will also serve org-wide community health files from this repo —
`SECURITY.md`, `CONTRIBUTING.md`, issue templates — to any repo that lacks its
own. The security policies are deliberately not consolidated that way: they
differ per tool in exactly the lines that matter, describing what that binary
can reach and what it stores, and an org-wide default would replace the useful
part with boilerplate. The reasoning is written down in `~/notes/dry_boundary.md`.
