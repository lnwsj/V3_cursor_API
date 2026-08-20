# Green Cutdee Project Memory Master

Project ID: green.cutdee.com
Project Name: Green Cutdee / V3_cursor_API
Repo Root: /Users/sj88/Documents/codex/V3_cursor_API
Environment: local-source + production-observation
Current Version: UI v1.1.1; API 1.2.0 / f6299fa; repo HEAD ba921c0
Notion Hub: https://www.notion.so/3435a17a475f818bae05c4dca1bb6aba
Project Rules: Global Operating Rules; project registry not resolved
API Base URL: https://green.cutdee.com/v3api
Last Updated: 2026-08-20

## Durable facts

- Production and local refactor are different snapshots: production Gateway `1.2.0/f6299fa`; code baseline `25e1032`; repo HEAD `ba921c0` is docs-only.
- Public UI root and API proxy are different surfaces. API schema is `/v3api/openapi.json`; API JSON liveness is `/v3api/healthz`.
- Public root `/healthz` and `/openapi.json` return frontend HTML; `/api/openapi.json` is 404 in the observed production surface.
- Dedicated Server services run from `/opt/v3-cursor-api` on gateway 8788 and worker 8789; actual per-job worker mapping still needs authenticated media evidence.
- Current cluster observation: 6 configured, 4 enabled/healthy, 2 disabled, active jobs 0, capacity 6.
- Local Gateway refactor remains release-blocked by lifecycle/import/auth/upload/dispatch/download issues.
- Unit tests are timing-sensitive in the worker lifecycle test: one first full run failed, three focused reruns passed, next full run passed 45.
- No raw token/password is allowed in this memory or any project artifact.

## Historical boundary

Historical reports and benchmarks remain evidence snapshots. Always record their date, commit, runtime and whether they prove source, API, UI, or real media output.

## Latest note

[`2026-08-20.md`](2026-08-20.md)
