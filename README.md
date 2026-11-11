# excelano/.github

Org-wide defaults for the excelano repos: reusable CI workflows, composite
actions, and the commit standard. The policy lives here once, and each repo calls it instead of
carrying its own copy.

## Calling a workflow

A consumer repo's `.github/workflows/ci.yml` is a few lines:

```yaml
name: ci

on:
  workflow_dispatch:

jobs:
  ci:
    uses: excelano/.github/.github/workflows/go-ci.yml@main
```

The trigger stays in the caller, and it is `workflow_dispatch`: a run happens
when a person asks for one, before a release or after a change worth testing.
What a build checks is the fleet's business. Pass `with:` inputs for the few
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

The actions live under `.github/actions/`, one directory each, and every
`action.yml` carries a `description` saying what it does and what it takes.

What stays in the caller is what genuinely differs. `mac-photograph` installs no
association, builds no release and makes no demo container, because which of
those a repo needs is the repo's business; it starts at the point where there is
something worth photographing.

## Why one host per org

The anderix repos call `anderix/.github` instead of this repo, even for an
identical workflow. Tools under anderix may not depend on excelano, so each org
holds its own copy of the policy; that is a boundary, not the per-repo copying
this repo exists to prevent.

This repo must stay public. A private one cannot serve reusable workflows to
public callers.

## The commit standard

`CONTRIBUTING.md` holds the commit message convention every repo in the org
follows. GitHub links it from any repo without a `CONTRIBUTING.md` of its own,
so no repo carries a copy.

## Community health files

GitHub serves org-wide community health files from this repo to any repo that
lacks its own, which is how `CONTRIBUTING.md` and the default `SECURITY.md`
reach every repo. A tool that authenticates, reaches an external system or
accepts network input carries its own `SECURITY.md`, because policies differ per
tool in the lines that matter: what that binary can reach and what it stores.
The default here covers the repos with nothing of that kind to describe.
