Requirements ledger: [REQUIREMENTS.md](REQUIREMENTS.md) (private, untracked on public checkouts).

# Maintainer Notes

- This fork stays on Cloudreve 3.8.3 (DB schema 3.8.1), with frontend pinned by the assets submodule. Preserve existing API routes, share IDs, configuration and data compatibility.
- Frontend work lives in assets. Generic deployment tooling lives in deploy. Do not commit data, credentials, production configuration or private ledgers.
- Build and verify through the custom GitHub workflow. Keep original licensing and upstream history.
- Use the existing React/Material UI/Go dependencies. Do not add new dependencies without approval.

## Change Timeline

### 2026-11-01 Custom 3.8.3 distribution

- Pin the frontend fork with the accepted share page, shared app theme and direct file workflows.
- Replace upstream publishing jobs with the custom build workflow and verified Linux release packages.
- Add deploy/cloudreve-manager.py for systemd, release switching, rollback and consistent private export/restore.
- Data compatibility: unchanged backend/API/schema; executable-relative data links preserve the existing data root.
- Verified before source publication: 123 frontend tests, 4 share-route tests, and Linux deployment suite (57 passed, 1 Windows-only skip); browser checks used an isolated data copy.
- Operational deployment records, private data fingerprints and backup locations belong in the installation's README/AGENTS/REQUIREMENTS, never in this public repository.
