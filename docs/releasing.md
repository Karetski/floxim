# Releasing

Releases are published by `.github/workflows/release.yml` when a `vX.Y.Z` tag is pushed (ADR 0016). This page lists the one-time setup and the steps for each release.

## One-time setup

1. **PyPI trusted publisher.** On pypi.org, open *Your account → Publishing* and add a pending GitHub Actions publisher: project name `floxim`, repository `Karetski/floxim`, workflow `release.yml`, environment `pypi`. A pending publisher does not reserve the name; it becomes the project's publisher on the first upload, and is invalidated if someone else registers `floxim` first, so publish soon after adding it.
2. **GitHub environment.** In the repository's *Settings → Environments*, create an environment named `pypi`. Adding yourself as a required reviewer makes every upload wait for your approval.

## Each release

1. On `main`, with CI green, set `__version__` in `src/floxim/__init__.py` to the new version.
2. Add a `## [X.Y.Z] - YYYY-MM-DD` entry at the top of `CHANGELOG.md` (with its link at the bottom), describing the changes for users.
3. Commit both as `Release X.Y.Z` and push.
4. Tag and push the tag:

   ```sh
   git tag -a vX.Y.Z -m "Floxim X.Y.Z"
   git push origin vX.Y.Z
   ```

5. Watch the *Release* workflow. It checks that the tag matches the version and the changelog has an entry, runs the tests, builds the wheel, sdist and `floxim.pyz`, uploads the wheel and sdist to PyPI (after your approval, if the environment requires it), and creates the GitHub release with all three files and the changelog entry.
6. Check the result on clean Linux and macOS machines:

   ```sh
   pipx install floxim==X.Y.Z
   floxim --version
   ```

   Then follow the README quickstart as written.

A version on PyPI cannot be replaced. If a release is broken, fix it and release the next patch version. If the workflow fails before the PyPI upload, delete the tag (`git push --delete origin vX.Y.Z` and `git tag -d vX.Y.Z`), fix the problem, and tag again.
