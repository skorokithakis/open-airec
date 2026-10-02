# Development

This guide is for people who work on `airec` itself. To use the recorder, see
the [README](../README.md).

## Install from source

```bash
git clone https://github.com/skorokithakis/open-airec.git
cd open-airec
uv venv
uv pip install -e .
```

The command is now `.venv/bin/airec`. To install your local copy as the
`airec` command for your user, run `uv tool install -e .`.

## Test

The tests use a fake Bluetooth connection. They do not need a recorder.

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q src tests
```

The `recordings/` and `artifacts/` folders are ignored by Git, so you cannot
commit recordings or device logs by accident.

## Research

[Research](research/) holds the Bluetooth protocol, how it was found, test
results and technical limits. Read it before you change protocol behavior.

## Release

Releases use [Release Please](https://github.com/googleapis/release-please)
and are published to PyPI as `open-airec`.

1. Write commit messages in the
   [Conventional Commits](https://www.conventionalcommits.org/) format, for
   example `feat: add storage command` or `fix: handle short catalog`. Release
   Please ignores other commits.
2. When `feat:` or `fix:` commits reach `main`, Release Please opens or updates
   a release pull request. This pull request changes the version and
   `CHANGELOG.md`.
3. Merge the release pull request. Release Please creates a `vX.Y.Z` tag and a
   GitHub release.
4. The tag starts the `publish-pypi.yml` workflow. It runs the tests, builds
   the package and publishes it to PyPI with trusted publishing.

Before 1.0, a breaking change (`feat!:`) increases the minor version, and
`feat:` and `fix:` increase the patch version.
