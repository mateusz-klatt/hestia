# Dependency caps

`.github/dependabot.yml` opens at most two pull requests per ecosystem, on a weekly schedule. Minor and patch updates are grouped. A library listed here is ignored for the next major (SQLAlchemy also ignores the 2.1 minor line) so Dependabot does not propose a bump this tree would reject.

Versions below were checked on 2026-11-22.

## npm (`ui/`)

| Package | Stay on | Skipped | Reason |
| --- | --- | --- | --- |
| typescript | 6.0.3 | 7.0.2 | `typescript-eslint@8.73.0` (latest) peers `typescript >=4.8.4 <6.1.0`. TypeScript 7 cannot be installed alongside the current linter. |

These majors were taken in the same refresh, with no application source changes: `vitest` 4.1.12 → 5.1.1, `jsdom` 29.1.1 → 31.0.1, `vite` 8.3.5 → 8.3.8. `eslint` 10.15.0 and `typescript-eslint` 8.73.0 were already the current releases. jsdom 31 requires Node `^22.22.2 || ^24.15.0 || >=26.0.0`, which is now the `engines` range. CI and the image use `node:22`, and the current 22.x line satisfies that range.

## Python (`requirements.txt`)

| Package | Pin | Skipped | Reason |
| --- | --- | --- | --- |
| cryptography | `>=50.0.2,<51` | no 51.x release | Already on the latest 50.x. The next major is not taken automatically; hestia only needs the hazmat cipher API. |
| aiohttp | `>=3.14.5,<4` | no 4.x release | Stay on 3.x. 3.14.5 logs `NotAppKeyWarning` for `request[key] = ...`; the contract tests pass without code changes. |
| SQLAlchemy | `>=2.0.58,<2.1` | 2.1.9 | 2.1 is a feature release (semver minor). The ORM and migration line stays on 2.0. Dependabot ignores both minor and major updates so 2.1 is not proposed; 2.0.x patches still are. |
| pyserial | `>=3.5,<4` | no 4.x release | 3.5 is the current release. The existing `<4` ceiling stays. |
| alembic | `>=1.20.1,<2` | no 2.x release | Floor raised inside 1.x. Migrations stay on alembic 1. |
| pydantic | `>=2.14.4,<3` | no 3.x release | Floor raised inside 2.x. The public DTO models stay on pydantic 2. |
| icalendar | `>=7.3.2,<8` | 8.0.0a1 only | The previous `<8` range already resolved to 7.x. The floor now matches 7.3.2. 8.x is still a prerelease, so the major stays ignored. |

## GitHub Actions

`actions/checkout`, `actions/setup-node`, and `actions/setup-python` moved from `@v6` to `@v7`. The v7 releases switch the action runtime to ESM and do not remove the inputs these workflows use (`fetch-depth`, `node-version`, `cache`, `cache-dependency-path`, `python-version`). `docker/setup-buildx-action@v4`, `docker/login-action@v4`, `docker/metadata-action@v6`, `docker/build-push-action@v7`, and `SonarSource/sonarqube-scan-action@v8` are already the current major tags. No action major is capped.
