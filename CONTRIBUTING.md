# Contributing

## Commit messages

Commit subjects follow [Conventional Commits](https://www.conventionalcommits.org/):
a type, an optional scope in parentheses, `!` for a breaking change, a colon,
and a description, as in `fix(store): refuse plain HTTP to a remote server`.

`feat`, `fix` and `perf` mark a change someone using the software would
notice. The release tooling turns these subjects into the release notes and
the package changelog with the type removed, so the description is written for
the person installing the release.

`build`, `chore`, `ci`, `docs`, `refactor`, `style` and `test` mark everything
else: tooling, dependencies, workflows, documentation, tests, and code that
changes no behaviour. These stay out of the release notes.
