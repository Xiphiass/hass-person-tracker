# hass-person-tracker

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues for Xiphiass/hass-person-tracker, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` at repo root, ADRs under `docs/adr/`. See `docs/agents/domain.md`.

## Workflow

- Build every issue on a feature branch (never commit work directly to `main`).
- After the work passes local tests and formatting (black), open a PR against `main` from the feature branch.
