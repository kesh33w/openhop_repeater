## What and why

<!-- What changes for an operator or user, and why. Link any issue. -->

## How it was tested

<!-- Tests added or updated. Any manual or bench checks, and what they did not cover. -->

## Checklist

- [ ] `pre-commit run --all-files` passes locally
- [ ] Behaviour change has a test that fails without it
- [ ] `CHANGELOG.md` updated under **Unreleased**
- [ ] Feature docs and `docs/fork/README.md` updated for new or changed config keys
- [ ] `repeater/web/openapi.yaml` updated for API changes (the UI client is generated from it)
- [ ] Cross-repo changes: matching core/UI/hub PRs linked and merged in order
- [ ] No secrets, live config or credentials committed
