# Green Cutdee Project Docs Master

Project ID: green.cutdee.com
Project Name: Green Cutdee / V3_cursor_API
Repo Root: /Users/sj88/Documents/codex/V3_cursor_API
Environment: local-source + production-observation
Current Version: UI v1.1.1; API 1.2.0 / f6299fa; repo HEAD ba921c0; code baseline 25e1032
Notion Hub: https://www.notion.so/3435a17a475f818bae05c4dca1bb6aba (Global Operating Rules; exact project registry page not found)
Project Rules: Global Operating Rules above; project-specific registry/rules page requires explicit resolution
API Base URL: https://green.cutdee.com/v3api
Last Updated: 2026-08-20

## Latest pointers

- Current detailed study: [`reports/green_cutdee_project_restudy_20260820_095321/report.md`](reports/green_cutdee_project_restudy_20260820_095321/report.md)
- Current HTML report: [`reports/green_cutdee_project_restudy_20260820_095321/report.html`](reports/green_cutdee_project_restudy_20260820_095321/report.html)
- Current source/live audit: [`V3_CURSOR_API_CURRENT_STATE_AUDIT_TH.md`](V3_CURSOR_API_CURRENT_STATE_AUDIT_TH.md)
- Latest daily note: [`index/2026-08-20.md`](index/2026-08-20.md)
- Latest memory note: [`memory/2026-08-20.md`](memory/2026-08-20.md)
- Latest RCA: [`debug_db/2026-08-20.md`](debug_db/2026-08-20.md)
- Latest timesheet: [`timesheet/2026-08-20.md`](timesheet/2026-08-20.md)

## Source and runtime boundary

- Public UI: `https://green.cutdee.com/`
- Public API proxy: `https://green.cutdee.com/v3api/`
- OpenAPI: `https://green.cutdee.com/v3api/openapi.json`
- JSON API liveness: `https://green.cutdee.com/v3api/healthz`
- Dedicated Server path: `/opt/v3-cursor-api`
- Gateway runtime: `127.0.0.1:8788`
- Worker runtime: `127.0.0.1:8789`
- Production release: API `1.2.0`, commit `f6299fa`
- Local code under audit: `25e1032`; repo HEAD `ba921c0` is docs-only on top

Root public `/healthz` and `/openapi.json` are frontend HTML catch-all paths; they are not the JSON API probe/schema. See the detailed report before using any render/download example.

## Required closeout

Update the relevant daily and master files, add a report, record validation and RCA, keep evidence paired, and never store raw secrets.
