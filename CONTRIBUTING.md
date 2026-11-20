# Contributing to HydraHop

This guide covers the four repositories released together: openhop_repeater (this repo), openhop_core, openHop_RepeaterUI and hydrahop. For upstream OpenHop work, contribute to [openhop-dev](https://github.com/openhop-dev) directly. Questions and bug reports for any of the four go in the [hub's issues](https://github.com/kesh33w/hydrahop/issues).

## Branches

| Branch | Meaning |
|---|---|
| `kesh/bridge` | Main line. Always what production runs, or is about to run. Protected by CI. |
| `<type>/<short-topic>` | Short-lived work branches, e.g. `fix/cad-timeout` or `feat/rx-identities`. Branch from `kesh/bridge` and merge back through a PR. |
| `sync/upstream-YYYYMMDD` | Upstream merges ([procedure](docs/fork/upstream-sync.md)). |
| `dev`, `main` | Mirrors of upstream; never commit fork work there. |

- Never rewrite history on `kesh/bridge`: no force-push, rebase or amend.
- Delete a work branch once it has merged.

## Making a change

1. Branch from `kesh/bridge`.
2. Make the change and add or update tests. A behaviour change needs a test that fails without it.
3. Run the gate locally:

   ```sh
   python3 -m venv .venv
   .venv/bin/pip install -e '.[dev]'
   .venv/bin/pre-commit run --all-files
   ```

   The gate runs `ruff check`, `ruff format --check`, bandit, the OpenAPI contract check and the full test suite.
4. Update the docs:
   - `CHANGELOG.md` under **Unreleased**;
   - the feature's doc in `docs/`;
   - [docs/fork/README.md](docs/fork/README.md) if you add or change a feature or config key;
   - `repeater/web/openapi.yaml` for any API change (the contract check enforces this).
5. Open a PR into `kesh/bridge` and fill in the template. Merge once CI passes.

### Cross-repository changes

- **Core and repeater together:** merge core first, then point the repeater's core pin at the new core commit in the same repeater PR (next section). CI installs exactly that commit.
- **API changes used by the web UI:** update `openapi.yaml` here, then run `npm run api:generate` in the UI repo, which regenerates the typed client. Then rebuild the bundle (next section).
- **Internet Link or enrollment changes:** run the hub's tests against this repo too (see the hub README).

### Core pin

`pyproject.toml` pins `openhop_core` to an exact commit, never a branch, so every install of a given repeater commit gets the same core. Both dependency lines (with and without `[hardware]`) carry the same SHA; change them together:

```sh
sed -i "s|openhop_core.git@[0-9a-f]\{40\}|openhop_core.git@$(git -C ../openhop_core rev-parse HEAD)|" pyproject.toml
```

Bump it whenever the repeater needs a newer core, and in every release PR (to core's release commit).

### Web UI bundle

`repeater/web/html/` is generated; never edit it by hand. With the UI repo checked out next to this one:

```sh
cd ../openHop_RepeaterUI
npm ci && npm run api:generate && npx vitest run && npx vue-tsc --build
npx vite build          # writes ../openhop_repeater/repeater/web/html
cd ../openhop_repeater
git ls-files -d repeater/web/html | xargs -r git checkout --   # keep older hashed assets
git add repeater/web/html && git commit -m "Bundle <what> from UI <ui-commit>"
```

Previously built hashed assets are kept, so a browser tab left open during an upgrade can still load its chunks.

## Commit messages

- **Subject:** imperative and under about 72 characters, saying what changes for the user or operator ("Keep one-way channel mappings one-way …").
- **Body:** explain the problem and why the fix is right. Reference the failure scenario where there is one.
- **Mechanical changes** such as formatting go in their own commit. Add that commit to `.git-blame-ignore-revs`.

## Releases

The four repositories are released together as one **HydraHop** version, using [semantic versioning](https://semver.org/):

| Bump | When | Example |
|---|---|---|
| PATCH (1.0.**1**) | Fixes only; safe to deploy without reading the config notes | a sensor read fix |
| MINOR (1.**1**.0) | New features or settings, all opt-in or backward compatible | the antenna identities setting |
| MAJOR (**2**.0.0) | Something an operator must change by hand: config format, compatibility with older nodes | renaming a config section |

Every repository gets the same tag, `hydrahop-vX.Y.Z`. The repeater's version (`repeater.__version__`, the observer label and the web UI) comes from that tag through setuptools-scm. Releases up to 19 Nov 2026 used date tags (`kesh-YYYY.MM.DD`); they stay as history.

1. Make sure `kesh/bridge` is green in every repo and the bundled UI is current.
2. In each repo, move the **Unreleased** changelog entries into a new version section, and merge that through a PR. Merge and tag core first; the repeater's release PR then pins core to that tagged commit.
3. Tag core, the web UI and the repeater: `git tag -a hydrahop-vX.Y.Z -m "HydraHop X.Y.Z"`, then `git push origin hydrahop-vX.Y.Z`.
4. In the hub:
   - pin `components.current.json` to those tags' commits;
   - update `docs/upgrade.md`;
   - tag the hub.
5. Publish a GitHub Release for each tag with that version's changelog section.

Always keep the `hydrahop-` prefix. Bare tags such as `1.2.0` or `v1.2.0` start upstream's Docker publish workflow.

## Deploying

Deployment is separate from merging, and a person always does it deliberately. Follow the hub's [upgrade guide](https://github.com/kesh33w/hydrahop/blob/kesh/openhop-bridge/docs/upgrade.md). Never copy files into an installed virtualenv: the running code must always match a commit.
