# SoloBot Git Workflow

This repository is maintained as a private working copy of the upstream SoloBot code.

## Repository Roles

- `origin` is the owner repository: `https://github.com/Viroopadas/Solo_bot`.
- `upstream` is the vendor repository: `https://github.com/Vladless/Solo_bot`.
- Push local work only to `origin`.
- Fetch new vendor releases only from `upstream`.
- Keep `origin/dev` as a copy of the vendor `dev` branch unless there is a separate reason to customize it.

Do not push to `upstream`. It is the place where the original SoloBot code was purchased and released, not the project owner's working repository.

## Current Working Model

The active production code should live on `origin/main`.

Local and production changes must be committed in `origin` before deployment. Runtime files must stay out of git:

- `config.py`, `.env`, `alembic.ini`;
- `.license_state`;
- `.venv/`, `venv/`;
- `logs/`, `__pycache__/`, `.ruff_cache/`;
- `storage/modules_state.json`;
- module tokens and caches.

## Updating From Vendor Releases

Vendor `main` is the stable release line. Vendor `dev` may contain a newer version before it reaches `main`.

1. Fetch both repositories:

   ```bash
   git fetch origin --tags
   git fetch upstream --tags
   ```

2. Create an update branch from current `origin/main`:

   ```bash
   git switch main
   git pull --ff-only origin main
   git switch -c update-from-upstream-YYYY-MM-DD
   ```

3. Bring in the vendor release:

   ```bash
   git merge upstream/main
   ```

   Or, when intentionally testing the vendor development version:

   ```bash
   git merge upstream/dev
   ```

   Resolve conflicts by preserving local production changes unless the vendor change clearly replaces them.

4. Run focused checks.

5. Merge or fast-forward the tested result into `origin/main`, then deploy.

## Production Rule

Production should point to a commit that exists in `origin`.

Before changing production:

```bash
git fetch origin upstream --tags
git status --short --branch
git rev-parse HEAD
```

After deployment, local and production should show the same commit hash for managed git files. Server-only runtime files may remain different.

## History Note

On 2026-07-06 the local and production copies were found with `origin` incorrectly pointing to `https://github.com/Vladless/Solo_bot`.

The correct setup is:

```bash
git remote -v
# origin   https://github.com/Viroopadas/Solo_bot
# upstream https://github.com/Vladless/Solo_bot
```

`Viroopadas/Solo_bot` was recreated as a proper fork of `Vladless/Solo_bot`, with both `main` and `dev` copied from the vendor repository. Local production commits are kept on top of the vendor release history and pushed to `origin`.
