# Release Process

This document describes the automated release process for QQcode.

## Overview

QQcode uses GitHub Actions to automate building and releasing binaries across multiple platforms. The process ensures that:

1. Binaries are built consistently for all supported platforms
2. ZIP files are created in the format expected by the Zed extension
3. Releases are automatically published to GitHub Releases and PyPI

## Workflows

### 1. `build-and-upload.yml` - Development Builds

**Triggers:**
- Push to `main` branch
- Pull requests
- Manual dispatch

**Purpose:**
- Builds binaries for all platforms
- Uploads artifacts for testing (expire after 90 days)
- Does NOT create releases

**Artifacts:**
- `qqcode-acp-{os}-{arch}-{version}` (binary only)

### 2. `release-binaries.yml` - Production Releases

**Triggers:**
- When a GitHub Release is published
- Manual dispatch (with tag input)

**Purpose:**
- Builds production binaries for all platforms
- Creates ZIP files in the format: `qqcode-acp-{os}-{arch}-{version}.zip`
- Automatically uploads ZIPs to the GitHub Release
- Binary inside ZIP is renamed to `qqcode-acp` (or `qqcode-acp.exe` for Windows)

**Platforms:**
- `darwin-aarch64` (Apple Silicon)
- `darwin-x86_64` (Intel Mac)
- `linux-x86_64`
- `windows-x86_64`

### 3. `release.yml` - PyPI Publishing

**Triggers:**
- When a GitHub Release is published
- Manual dispatch

**Purpose:**
- Builds Python wheel and sdist
- Publishes to PyPI

## Release Steps

### Automated Process (Recommended)

1. **Bump Version:**
   ```bash
   uv run scripts/bump_version.py [major|minor|patch]
   ```
   This updates:
   - `pyproject.toml`
   - `distribution/zed/extension.toml`
   - `.vscode/launch.json`
   - `vibe/core/__init__.py`
   - `tests/acp/test_initialize.py`

2. **Commit and Push:**
   ```bash
   git add .
   git commit -m "Bump version to vX.Y.Z"
   git push origin main
   ```

3. **Create and Push Tag:**
   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

4. **Create GitHub Release:**
   - Go to https://github.com/qnguyen3/qqcode/releases/new
   - Select the tag you just created
   - Generate release notes (or write custom notes)
   - Click "Publish release"

5. **Automated Actions:**
   - `release-binaries.yml` will automatically:
     - Build all platform binaries
     - Create ZIP files
     - Upload ZIPs to the GitHub Release
   - `release.yml` will automatically:
     - Build Python package
     - Publish to PyPI

### Manual Release (Fallback)

If you need to manually trigger the release workflow:

1. Go to https://github.com/qnguyen3/qqcode/actions/workflows/release-binaries.yml
2. Click "Run workflow"
3. Enter the tag (e.g., `v1.0.0`)
4. Click "Run workflow"

The workflow will build and upload ZIPs to the specified tag's release.

## Verification

After release, verify:

1. **GitHub Release:**
   - All platform ZIPs are attached
   - ZIP names match format: `qqcode-acp-{os}-{arch}-{version}.zip`

2. **ZIP Contents:**
   ```bash
   unzip -l qqcode-acp-darwin-aarch64-1.0.0.zip
   # Should show: qqcode-acp (or qqcode-acp.exe)
   ```

3. **PyPI:**
   - Package is published: https://pypi.org/project/qqcode/

4. **Zed Extension:**
   - URLs in `distribution/zed/extension.toml` are accessible
   - Version numbers match

## Troubleshooting

### Build Failures

If a platform build fails:
1. Check the Actions logs for that specific runner
2. Fix the issue in code
3. Create a new tag/release to trigger rebuild

### Missing ZIPs

If ZIPs are not uploaded to the release:
1. Check that the GitHub Release was created BEFORE the workflow ran
2. Ensure `GITHUB_TOKEN` has write permissions
3. Manually trigger the workflow with the tag name

### Version Mismatches

If versions don't match across files:
1. Always use `bump_version.py` script
2. Never manually edit version numbers
3. The script updates all necessary files automatically

## Architecture Notes

**Why two build workflows?**
- `build-and-upload.yml` for development/testing (artifacts)
- `release-binaries.yml` for production (GitHub Releases)

**Why rename binary?**
- PyInstaller outputs `vibe-acp` (internal name)
- Zed extension expects `qqcode-acp` (public name)
- Workflow handles the rename automatically

**Why get version from pyproject.toml?**
- Single source of truth
- Works on all platforms (tomllib is built-in to Python 3.11+)
- Consistent with `bump_version.py` script
