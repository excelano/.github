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

## Calling an action

A reusable workflow is a whole job and brings its own runner. Where the shared
thing is a handful of steps inside a job the caller already has — a job that has
checked out, built, and installed something first — it is a composite action
instead:

```yaml
      - name: The integration goes on and comes off cleanly
        uses: excelano/.github/.github/actions/windows-install-check@main
        with:
          associations: '[{"extension": ".dclx", "progId": "Excelano.Segler.Archive"}]'
```

| Action | What it does |
| --- | --- |
| `windows-install-check` | Runs the Windows install scripts against the registry and reads back: what goes on comes off, a `UserChoice` naming this application goes with it, and another application's does not |
| `store-package` | Builds the unsigned MSIX the Store takes, keeps it, and attaches it to the release when there is a tag |
| `mac-release-binary` | Builds the universal binary at the floor the Info.plist declares, checks every slice is present, and attaches it for the Mac to sign |
| `powershell-parses` | Parses every `.ps1` under a path and fails on one that will not, naming the file and the line |
| `store-screenshots` | Raises the runner's desktop, runs `packaging/windows/shots.ps1`, and uploads what it wrote |
| `mac-store-screenshots` | Runs `packaging/macos/shots.sh` once per language and uploads what it wrote |

Each of these was the same steps in three or more repos, or — which turned out
to matter more — was in three and missing from three, and the version that was
missing is the one that would have caught something. `windows-install-check`
found the same class of defect in four uninstallers on the day it was shared.

What stays in the caller is what genuinely differs. Neither screenshot action
installs an association, builds a release or makes a demo container, because
which of those a repo needs is the repo's business; each starts at the point
where there is something worth photographing.

The two are separate rather than one action with a runner switch, because
almost nothing about them is shared: one raises a desktop resolution through a
Windows cmdlet and the other satisfies a macOS privacy gate, and a single file
carrying both would be two scripts under one name.

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
