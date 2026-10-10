# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Project rules

- For any codebase search (files, symbols, usages, "where/how is X"), delegate to the `haiku-scout` subagent instead of searching yourself. Use its report to act.
- Also delegate cheap bulk work (classification, extraction, summarizing files) to `haiku-scout`.
- Backend work goes phase by phase per `PRD-backend.md` §10: `backend-coder` (sonnet) writes, `phase-reviewer` (opus) accepts, then tests in the container and one commit per phase with a Russian message (`бэк: ...`).
- Do not pull Docker images yourself; tell the user which ones are needed.

# Repo ownership and git (bombomteam/prohodnaya)

- Folder owners: `backend/` — goldslugger762 (this user), `frontend/` and `keycloak/` — scuf77, `face/` — DevOps. Edit only `backend/`. Never change other folders or the shared `compose.yaml`, `.env.example`, `.github/`, `deploy/` — write the needed change as a request for the owner instead.
- Before work: `git pull --rebase`. Work in your own branch; into `main` only via PR. Never `git push --force`. A push to `main` deploys to the stand, so run backend tests first.
- To bring `main` into a branch whose commits are already pushed, use `git merge origin/main` (rebasing pushed commits would need a force push).
- Never commit `.env`, passwords, tokens, keys, photos/videos (except `case_prerekvizity/`), models, archives, DB dumps, files > 1 MB. Check `git status` and `git add` files by name, not `-A` or a whole directory.
- Small commits: `бэк: …` (`фронт: …`, `keycloak: …` for others).
- CI runs on every push and PR (tests of your part, secret and junk scan); the result is the `ci-ok` check. Into `main` only via a PR with green `ci-ok`. Red CI: open Actions, find the failed step, fix it in your branch. The backend CI job starts Postgres 16 and fails if any test is SKIPPED, so every test must really run there (the `<db>_test` database is created by `tests/conftest.py`; the CI user can CREATE DATABASE). Still run the tests in the container before pushing.
- Push to `main` deploys to the stand https://hack-detect.duckdns.org (login: https://auth.hack-detect.duckdns.org). A failed deploy rolls back automatically; manual rollback: Actions → Rollback → Run workflow. `git push --force` is forbidden.
- Stand: `/api` goes through Caddy straight to `backend` on the same origin (no CORS, body limit 25 MB); `FRONTEND_MODE=api`. Keycloak issuer `https://auth.hack-detect.duckdns.org/realms/construction` (`KC_PUBLIC_URL`), token/JWKS from inside via `http://keycloak:8080` (`KC_INTERNAL_URL`).
- Full rules: section «Git, CI и стенд» in `AGENTS.md`.

# Commands

Stack (frontend + keycloak + db + backend) from the repo root; `.env` from `.env.example` with `POSTGRES_*`/`DATABASE_URL` and the four `KC_*_PASSWORD` filled (without the KC passwords compose refuses to start any service). `COMPOSE_PROFILES=backend` enables db and backend. The `face` service is in compose (profile `backend`); if it is not running, person creation returns 503 and passes are denied with `FACE_SERVICE_UNAVAILABLE`.

```
docker compose up -d --build db backend
docker compose exec -T backend python -m pytest -q -p no:cacheprovider tests
docker compose exec -T backend python -m pytest -q -p no:cacheprovider tests/test_main.py::test_health
curl localhost:8000/health        # API docs: localhost:8000/docs
```

There is no Python 3.12 on the host (system python3 is 3.9): run backend tests inside the container.

# Backend architecture (`backend/app`)

- FastAPI + **sync** SQLAlchemy 2 (PRD-mandated; the face client is sync httpx). Only assistant routes are `async` (the `llm` contract is async).
- Layers: `api/` (routes, no SQL/logic) → `services/` (business logic) → `repositories/` (DB queries; `repositories/queries.py` is the single source of numbers for analytics, reports and the assistant). `schemas/` pydantic, `models/` ORM, `core/` config/exceptions/handlers/logging/auth/crypto, `dependency.py` holds `XDep = Annotated[...]` wrappers.
- All §8 routes are under `/api` (`api/router.py`); `/health` stays at the root.
- Error format everywhere: `{code, message}`, Russian `message`; raise `core.exceptions.ApiError`. Handlers in `core/handlers.py`.
- `core/auth.py` validates Keycloak JWTs (RS256 via JWKS, `iss`, `aud=access-api`, `exp`); keep the `current_user`/`require_role` signatures. `AUTH_DEV_BYPASS=true` gives a dev user with all roles (local only).
- Operator login routes `/api/auth/config|exchange|refresh|logout` (contract: `frontend/docs/backend-auth-handoff.md`) belong to the backend: proxy to the Keycloak token endpoint for the public client `panel`, re-validate the access token with the `core/auth.py` validator, `Cache-Control: no-store`, never log tokens, codes or PKCE verifiers. Workers never use Keycloak.
- `app/face/` is the thin HTTP client to the face module (contract `face/docs/api.md` v1); the module itself lives in `/face` and has another owner — don't touch `/face`. `app/llm/` is an interface owned by another teammate — don't implement it. Never add a fake "match"/canned answer; any face failure = deny. Tests monkeypatch `face` functions or the httpx transport.
- Face reason codes map to `FACE_<REASON>` (e.g. `FACE_NO_FACE`, `FACE_FACE_MISMATCH`) with the module's `reason.text`; `FaceRejected` (4xx bad_image/unsupported_format/too_large) → 422 on enrollment; `FaceUnavailable` → `FACE_SERVICE_UNAVAILABLE`. A pass is allowed only when the 6-digit code was confirmed AND the module returned `status == "ok"`.
- Route bodies still marked `# STUB` are placeholders from Ф1; `tests/test_no_stubs.py` (Ф8) must find none.
- The pass decision is server-side only: the code is one-shot and consumed by atomic `UPDATE ... WHERE status=... RETURNING`; every deny writes an `access_events` row (except `/device/pass-code` refusals: no session yet, log only).
- Pass flow (`services/passage.py`, PRD §8.4): controller finds the person (`GET /api/passages/people`) after checking their ID card and starts a session (`POST /api/passages {person_id, direction}`; rule failures → immediate decided/deny). The server creates a one-time 6-digit code; only the person's active phone gets it via `POST /api/device/pass-code` signed with its non-extractable ECDSA key (monotonic `ts`, replay → `REPLAYED`). The worker says the code, the controller types it (`POST /api/passages/{id}/code`; row locked `FOR UPDATE`, compared in the DB, max `PASS_CODE_MAX_ATTEMPTS`). Then `/face`: rules re-checked, device re-checked, commit, `face.verify_frames` outside any transaction, decision + shift + event in one transaction (`_finish`). The code is never returned to the tablet nor logged (`hide_parameters=True`). Business deny = 200 `decision=deny`; technical errors: 404 `SESSION_NOT_FOUND`, 403 `DEVICE_INVALID`, 409 `SESSION_EXPIRED`, 403 `SESSION_FOREIGN`.
- Tests needing a DB use the separate `<db>_test` database from `tests/conftest.py` (fixtures `client`, `db`); never write to the app DB from tests (the only exception is a read-only check of the app engine's `hide_parameters` in `test_passages.py`).

Frontend: see `frontend/AGENTS.md` before touching `frontend/`.

@AGENTS.md
