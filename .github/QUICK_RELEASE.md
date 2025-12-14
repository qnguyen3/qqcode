# Quick Release Guide

## TL;DR - One Command Release

```bash
# 1. Bump version and commit
uv run scripts/bump_version.py patch  # or minor, or major
git add .
git commit -m "Bump version to v$(uv run python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"

# 2. Tag and push
VERSION=$(uv run python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
git tag "v$VERSION"
git push origin main "v$VERSION"

# 3. Create GitHub Release (web UI or gh CLI)
gh release create "v$VERSION" --generate-notes

# Done! CI will automatically:
# - Build binaries for all platforms
# - Create and upload ZIP files to GitHub Release
# - Publish to PyPI
```

## Using GitHub CLI (Recommended)

Install: `brew install gh` (macOS) or see https://cli.github.com/

```bash
# Complete release in one go
VERSION=$(uv run python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
uv run scripts/bump_version.py patch
git add .
git commit -m "Bump version to v$VERSION"
git tag "v$VERSION"
git push origin main "v$VERSION"
gh release create "v$VERSION" --generate-notes --title "QQcode v$VERSION"
```

## What Happens Automatically

When you create a GitHub Release:

1. ✅ **Binaries Built** - All platforms (darwin, linux, windows)
2. ✅ **ZIPs Created** - Format: `qqcode-acp-{os}-{arch}-{version}.zip`
3. ✅ **GitHub Release Updated** - ZIPs automatically attached
4. ✅ **PyPI Published** - Package available via `pip install qqcode`

## Verify Release

```bash
# Check GitHub Release has all ZIPs
gh release view "v$VERSION"

# Download and test a ZIP
curl -LO "https://github.com/qnguyen3/qqcode/releases/download/v$VERSION/qqcode-acp-darwin-aarch64-$VERSION.zip"
unzip -l "qqcode-acp-darwin-aarch64-$VERSION.zip"
# Should show: qqcode-acp

# Check PyPI
pip index versions qqcode
```

## Rollback

If you need to delete a bad release:

```bash
# Delete GitHub Release and tag
gh release delete "vX.Y.Z" --yes
git tag -d "vX.Y.Z"
git push origin :refs/tags/vX.Y.Z

# Note: PyPI releases cannot be deleted, only yanked
# If needed: https://pypi.org/help/#yanked
```

## Troubleshooting

**Problem:** ZIPs not uploaded to release
**Solution:** Manually trigger workflow:
```bash
gh workflow run release-binaries.yml -f tag=vX.Y.Z
```

**Problem:** Version mismatch errors
**Solution:** Always use `bump_version.py` script, never edit versions manually

**Problem:** Build failed on specific platform
**Solution:** Check Actions logs, fix issue, create new release with patch version
