# Working in this repository

## Before every commit

Both must be clean, in this order:

```bash
poetry run pyright              # zero errors, always
poetry run python -m pytest     # ffmpeg/ffprobe on PATH for full coverage
```

`poetry run pre-commit install` (once per clone) makes git run pyright
automatically on every commit.

## Type checking

The codebase is pyright-clean at the default (standard) level and stays that
way: fix the type error, don't suppress it. A `# pyright: ignore[rule]` with a
specific rule is acceptable only in tests, for fakes and test-only attributes
that deliberately step outside the real types. Reach for `cast()` at genuine
boundaries (untyped third-party returns, test doubles), never to silence a
real mismatch.

## Conventions

Dependencies are managed with poetry (`poetry add`, `poetry add --group dev`);
`poetry.lock` is committed. Everything else — layout, cadence machinery, test
philosophy, UTC-on-disk rules — is in `docs/Development.md`. Documentation
lives in `docs/` and changes land in the same commit as the code they
describe. Don't write derived counts (test totals and the like) into docs or
comments; they go stale.
