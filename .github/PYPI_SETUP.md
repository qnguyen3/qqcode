# PyPI Publishing Setup Guide

This guide walks you through setting up automatic PyPI publishing for QQcode using **Trusted Publishing** (no API tokens needed!).

## Prerequisites

- [ ] GitHub repository: `qnguyen3/qqcode`
- [ ] PyPI account (create at https://pypi.org/account/register/)
- [ ] Admin access to the repository

## What is Trusted Publishing?

Trusted Publishing uses OpenID Connect (OIDC) to let GitHub Actions publish to PyPI **without storing API tokens**. It's more secure and the recommended approach.

## Step-by-Step Setup

### 1. Create PyPI Account (if needed)

1. Go to https://pypi.org/account/register/
2. Fill in your details
3. Verify your email address
4. Enable 2FA (recommended)

### 2. Set Up Trusted Publishing on PyPI

#### Option A: For First-Time Publishing (Pending Publisher)

Since `qqcode` hasn't been published to PyPI yet, you need to set up a "pending publisher":

1. **Log in to PyPI**: https://pypi.org/manage/account/

2. **Go to Publishing**: https://pypi.org/manage/account/publishing/

3. **Click "Add a new pending publisher"**

4. **Fill in the form:**
   ```
   PyPI Project Name:        qqcode
   Owner:                    qnguyen3
   Repository name:          qqcode
   Workflow name:            release.yml
   Environment name:         pypi
   ```

5. **Click "Add"**

6. **What happens next:**
   - The first time you create a GitHub Release, the workflow will publish to PyPI
   - PyPI will automatically create the `qqcode` project
   - Future releases will publish automatically

#### Option B: For Existing PyPI Project

If `qqcode` already exists on PyPI:

1. **Go to your project**: https://pypi.org/manage/project/qqcode/settings/publishing/

2. **Click "Add a new publisher"**

3. **Fill in the form:**
   ```
   Owner:                    qnguyen3
   Repository name:          qqcode
   Workflow name:            release.yml
   Environment name:         pypi
   ```

4. **Click "Add"**

### 3. Create GitHub Environment

1. **Go to your repository settings:**
   ```
   https://github.com/qnguyen3/qqcode/settings/environments
   ```

2. **Click "New environment"**

3. **Name it:** `pypi`

4. **Click "Configure environment"**

5. **Optional - Add protection rules:**
   - ✅ **Required reviewers**: Add yourself (prevents accidental releases)
   - ✅ **Deployment branches**: Only `main` or `tags`

6. **Click "Save protection rules"**

### 4. Test the Setup

#### Test Publishing to TestPyPI (Recommended First)

Before publishing to the real PyPI, test with TestPyPI:

1. **Create TestPyPI account**: https://test.pypi.org/account/register/

2. **Set up pending publisher on TestPyPI**: https://test.pypi.org/manage/account/publishing/
   - Same details as above

3. **Create test workflow** (optional - create `.github/workflows/test-pypi.yml`):
   ```yaml
   name: Test PyPI Release
   
   on:
     workflow_dispatch:
   
   jobs:
     test-release:
       runs-on: ubuntu-latest
       environment:
         name: test-pypi
       permissions:
         id-token: write
         contents: read
       
       steps:
         - uses: actions/checkout@v4
         - uses: actions/setup-python@v5
           with:
             python-version: "3.12"
         - uses: astral-sh/setup-uv@v5
         - run: uv sync --locked --dev
         - run: uv build
         - name: Publish to TestPyPI
           uses: pypa/gh-action-pypi-publish@release/v1
           with:
             repository-url: https://test.pypi.org/legacy/
   ```

4. **Run the test workflow manually**

5. **Check TestPyPI**: https://test.pypi.org/project/qqcode/

#### Test Real PyPI Publishing

1. **Create a test release:**
   ```bash
   # Make sure version is bumped
   uv run scripts/bump_version.py patch
   git add .
   git commit -m "Bump version for test release"
   git tag v1.0.1-test
   git push origin main v1.0.1-test
   
   # Create GitHub Release
   gh release create v1.0.1-test --prerelease --title "Test Release"
   ```

2. **Watch the workflow:**
   ```bash
   # Or go to: https://github.com/qnguyen3/qqcode/actions
   gh run watch
   ```

3. **Check PyPI:**
   ```
   https://pypi.org/project/qqcode/
   ```

4. **Test installation:**
   ```bash
   pip install qqcode
   qqcode --version
   ```

## Verification Checklist

After setup, verify:

- [ ] PyPI pending publisher is configured
- [ ] GitHub environment `pypi` exists
- [ ] Workflow file uses `environment: pypi`
- [ ] Workflow file uses `permissions: id-token: write`
- [ ] Repository check matches: `qnguyen3/qqcode`

## Troubleshooting

### Error: "Trusted publishing exchange failure"

**Cause:** Mismatch in PyPI publisher configuration

**Fix:**
1. Check PyPI publisher settings match exactly:
   - Owner: `qnguyen3`
   - Repo: `qqcode`
   - Workflow: `release.yml`
   - Environment: `pypi`
2. Re-run the workflow

### Error: "Environment protection rules not satisfied"

**Cause:** GitHub environment has required reviewers

**Fix:**
1. Go to: https://github.com/qnguyen3/qqcode/actions
2. Find the pending deployment
3. Click "Review deployments"
4. Approve the deployment

### Error: "Project name conflict"

**Cause:** Someone else already owns the `qqcode` name on PyPI

**Fix:**
1. Choose a different name (e.g., `qqcode-cli`)
2. Update `pyproject.toml`:
   ```toml
   [project]
   name = "qqcode-cli"
   ```
3. Update PyPI publisher configuration

### Warning: "Package already exists"

**Cause:** Trying to re-upload the same version

**Fix:**
- You cannot replace a version on PyPI
- Bump the version and create a new release

## Security Best Practices

✅ **DO:**
- Use Trusted Publishing (OIDC) - no API tokens needed
- Enable 2FA on your PyPI account
- Use GitHub environment protection rules
- Review deployments before they go live

❌ **DON'T:**
- Store API tokens in GitHub Secrets (outdated approach)
- Share your PyPI credentials
- Disable 2FA
- Allow public forks to trigger deployments

## Advanced: Manual Publishing (Fallback)

If Trusted Publishing fails, you can publish manually:

```bash
# 1. Build locally
uv build

# 2. Install twine
uv pip install twine

# 3. Upload to PyPI (will prompt for credentials)
uv run twine upload dist/*

# Or use API token:
# Create token at: https://pypi.org/manage/account/token/
uv run twine upload dist/* --username __token__ --password pypi-...
```

## Resources

- **PyPI Trusted Publishing Docs**: https://docs.pypi.org/trusted-publishers/
- **GitHub OIDC Docs**: https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/about-security-hardening-with-openid-connect
- **PyPA Publishing Guide**: https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/

## Summary

Once set up, your release process is:

```bash
uv run scripts/bump_version.py patch
git add . && git commit -m "Bump version"
git tag v1.0.1 && git push origin main v1.0.1
gh release create v1.0.1 --generate-notes
```

GitHub Actions will automatically:
1. ✅ Build the package
2. ✅ Publish to PyPI (no tokens needed!)
3. ✅ Upload binaries to GitHub Release
