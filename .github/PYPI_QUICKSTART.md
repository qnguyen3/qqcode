# PyPI Setup - Quick Start (5 Minutes)

## TL;DR - No Tokens Needed! 🎉

Modern PyPI uses **Trusted Publishing** - GitHub Actions authenticates directly with PyPI using OpenID Connect. No API tokens to manage!

## Quick Setup (3 Steps)

### Step 1: PyPI Pending Publisher (2 minutes)

1. Go to: https://pypi.org/manage/account/publishing/
2. Click **"Add a new pending publisher"**
3. Fill in:
   ```
   PyPI Project Name:    qqcode
   Owner:                qnguyen3
   Repository name:      qqcode
   Workflow name:        release.yml
   Environment name:     pypi
   ```
4. Click **"Add"**

✅ That's it for PyPI!

### Step 2: GitHub Environment (2 minutes)

1. Go to: https://github.com/qnguyen3/qqcode/settings/environments
2. Click **"New environment"**
3. Name: `pypi`
4. *Optional:* Add yourself as required reviewer (prevents accidental releases)
5. Click **"Save protection rules"**

✅ GitHub is configured!

### Step 3: Test It (1 minute)

Create a test release:
```bash
# The next release will automatically publish to PyPI
git tag v1.0.1
git push origin v1.0.1
gh release create v1.0.1 --generate-notes
```

Then check: https://pypi.org/project/qqcode/

## Visual Flow

```
┌─────────────────────────────────────────────────────────────┐
│  You: Create GitHub Release                                 │
│  → gh release create v1.0.1 --generate-notes                │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│  GitHub Actions: Triggered                                   │
│  → Workflow: .github/workflows/release.yml                  │
│  → Environment: pypi                                         │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│  GitHub: Generate OIDC Token                                 │
│  → Short-lived token (expires in minutes)                   │
│  → Contains: repo name, workflow, environment               │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│  PyPI: Verify Token                                          │
│  → Checks: owner=qnguyen3, repo=qqcode, workflow=release.yml│
│  → Matches pending publisher? ✅                             │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│  PyPI: Publish Package                                       │
│  → Creates project (first time)                             │
│  → Uploads new version                                       │
│  → Available at: pip install qqcode                          │
└─────────────────────────────────────────────────────────────┘
```

## Verification Checklist

After setup, check these 5 things:

- [ ] PyPI pending publisher exists: https://pypi.org/manage/account/publishing/
- [ ] GitHub environment `pypi` exists: https://github.com/qnguyen3/qqcode/settings/environments
- [ ] Workflow has `environment: pypi` ✅ (already configured)
- [ ] Workflow has `permissions: id-token: write` ✅ (already configured)
- [ ] Workflow has `if: github.repository == 'qnguyen3/qqcode'` ✅ (already configured)

## What You DON'T Need

❌ PyPI API tokens  
❌ GitHub Secrets  
❌ Manual uploads  
❌ Local credentials  

## Common Questions

**Q: Is this secure?**  
A: Yes! More secure than API tokens. The OIDC token expires in minutes and can only be used by your specific workflow.

**Q: What if it fails?**  
A: Check the workflow logs. Most common issue: typo in pending publisher configuration.

**Q: Can I test first?**  
A: Yes! Use TestPyPI (see full guide: `.github/PYPI_SETUP.md`)

**Q: How do I publish updates?**  
A: Just create a new release. The workflow runs automatically.

**Q: Can I manually publish if needed?**  
A: Yes, use `uv build && uv run twine upload dist/*`

## Complete Documentation

For detailed setup, troubleshooting, and TestPyPI instructions:
👉 **[.github/PYPI_SETUP.md](.github/PYPI_SETUP.md)**

## Next Steps

1. ✅ Set up PyPI pending publisher (2 min)
2. ✅ Create GitHub environment (2 min)
3. 🚀 Create your first release!
