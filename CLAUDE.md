# Rehearsal Planner — Project Guide for Claude

A band rehearsal planner: per-user song library + ordered set list, with Spotify search, album art lookup, and a time-budget bar (warn at 2h, max 2h30).

## Stack

- **Backend**: Flask (Python 3.9+), single `app.py`
- **Frontend**: Vanilla JavaScript inside `templates/index.html` (one ~2600-line file, no npm/no build tooling, SortableJS from CDN)
- **Database**: Neon Postgres
- **Auth**: Neon Auth (powered by **Better Auth**, NOT Stack Auth — historical confusion, see Gotchas)
- **Hosting**: Vercel (Hobby tier), auto-deploys on `git push origin main`
- **Live URL**: https://rehearsal-planner.vercel.app
- **Repo**: https://github.com/jmsyng/rehearsal-planner

## File Layout

```
rehearsal-planner/
├── app.py              # Flask app + all API routes (songs, setlist, tunings, spotify/youtube/album-art proxies)
├── auth.py             # JWT validation via Neon Auth JWKS; @require_auth decorator
├── db.py               # Postgres CRUD (fresh connection per request — DO NOT add pooling, see Gotchas)
├── schema.sql          # DDL for songs, set_lists, set_list_songs, user_tunings
├── init_db.py          # Run once to apply schema.sql to the Neon DB
├── requirements.txt    # Python deps
├── vercel.json         # Vercel routing — sends all requests to api/index.py
├── api/index.py        # Vercel serverless entry — `from app import app`
├── templates/index.html  # Entire frontend (HTML + CSS + JS)
├── static/             # Empty — Flask requires it but nothing's there
├── .env                # Local secrets (gitignored)
├── .env.example        # Template for .env
├── .gitignore
├── .vercelignore       # Excludes .env, init_db.py, schema.sql from deploys
└── CLAUDE.md           # This file
```

## Auth Flow

This took several iterations to get right — read this before touching auth code.

**Sign-up / sign-in (browser → Neon Auth):**
1. `POST {NEON_AUTH_BASE_URL}/sign-up/email` with `{email, password, name}` OR `/sign-in/email` with `{email, password}`
2. Response: `{token: "<opaque-session-token>", user: {...}}` + sets HttpOnly cookie `__Secure-neon-auth.session_token` (SameSite=None, cross-origin OK)
3. **The `token` in the body is NOT a JWT** — it's an opaque session token. To get a JWT:
4. `GET {NEON_AUTH_BASE_URL}/token` (cookie auto-sent via `credentials: 'include'`) → returns `{token: "<JWT>"}`
5. Store JWT in `sessionStorage` as `rp_access_token`, attach as `Authorization: Bearer <jwt>` to all `/api/*` calls

**JWT details:**
- Algorithm: **EdDSA** (Ed25519), NOT RS256
- Audience + Issuer: both are the **origin** of `NEON_AUTH_BASE_URL` (scheme + host only, no path) — e.g. `https://ep-xxx.neonauth.c-8.us-east-1.aws.neon.tech`
- Lifetime: 15 minutes (session cookie lasts 7 days)
- Claims: `sub` (user UUID), `email`, `role` ("authenticated"), `exp`, `iat`, `aud`, `iss`

**Silent JWT refresh:** `apiFetch()` helper in `index.html` wraps all backend API calls — on 401, calls `fetchNeonJwt()` to exchange the still-valid session cookie for a fresh JWT, then retries once. If the cookie is also dead, falls back to `signOut()`.

**Backend validation** (`auth.py`):
- Fetches JWKS from `NEON_AUTH_JWKS_URL` (cached 60 min)
- Uses `jwt.PyJWK(key_data).key` (algorithm-agnostic key loading)
- Verifies signature, exp, aud, iss
- Sets `g.user_id = payload["sub"]` for the route handler

## Database Schema

All app tables live in `public`; auth tables in `neon_auth` (managed by Neon, do not modify).

- `neon_auth."user"` — UUID `id`, email, name, etc. (Note: `"user"` is reserved SQL — always quote it)
- `songs` — composite PK `(id, user_id)`, FK to `neon_auth."user"(id) ON DELETE CASCADE`
- `set_lists` — one row per user (single active set list)
- `set_list_songs` — ordered join, references `songs(id, user_id)`
- `user_tunings` — custom tunings beyond the 4 hardcoded defaults

`user_id` is **UUID** everywhere (matches `neon_auth."user".id`), not TEXT. The first iteration used TEXT and FK'd to `neon_auth.users_sync` (a Stack-Auth-era table that doesn't exist in Better Auth) — both wrong.

## API Routes

All `/api/songs`, `/api/setlist`, `/api/tunings`, `/api/initial-songs` are `@require_auth`.
The proxy routes (`/api/spotify-search`, `/api/youtube-search`, `/api/album-art/<artist>/<track>`) are public (stateless, just forward to third-party APIs with hardcoded creds).

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/songs` | — | `[song, ...]` |
| POST | `/api/songs` | song | saved song (201) |
| PUT | `/api/songs/<id>` | song | updated song |
| DELETE | `/api/songs/<id>` | — | `{ok: true}` |
| GET | `/api/setlist` | — | `["song-id", ...]` |
| POST | `/api/setlist` | `[{song_id, position, plays}]` | `{ok: true}` |
| GET | `/api/tunings` | — | `["E standard", ...]` |
| POST | `/api/tunings` | `{tuning: "Open G"}` | `{ok: true}` |
| GET | `/api/initial-songs` | — | seeds the user with 37 default songs + returns them |

## Local Development

```bash
cd /Users/jimmy/ClaudeZone/rehearsal-planner
pip install -r requirements.txt   # only first time
python app.py                     # serves on http://localhost:5050
```

For schema changes:
```bash
# edit schema.sql, then:
python init_db.py
```

## Deployment

Push to main → Vercel auto-deploys (~30-60s):
```bash
git push origin main
```

Env vars on Vercel are set via the dashboard (Project → Settings → Environment Variables), NOT via `.env` (which isn't deployed). The local `.env` and Vercel env vars must be kept in sync manually.

## Environment Variables (required)

| Var | Where it comes from | Used by |
|---|---|---|
| `DATABASE_URL` | Neon Console → Connection Details (use the `-pooler` host) | `db.py` |
| `NEON_AUTH_BASE_URL` | Neon Auth panel (e.g. `https://ep-xxx.neonauth.c-8.us-east-1.aws.neon.tech/neondb/auth`) | `auth.py`, frontend meta tag |
| `NEON_AUTH_JWKS_URL` | Neon Auth panel (BASE_URL + `/.well-known/jwks.json`) | `auth.py` |
| `FLASK_SECRET_KEY` | `python3 -c "import secrets; print(secrets.token_hex(32))"` | Flask |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | Spotify Developer Dashboard | `app.py` (search proxy) |
| `YOUTUBE_API_KEY` | Google Cloud Console (YouTube Data API v3) | `app.py` (search proxy) |

`NEON_AUTH_COOKIE_SECRET` appears in some Neon Auth setup UIs but is **not used** by our architecture (it's for the Next.js SDK pattern where your server issues its own cookies — we don't).

## Gotchas / Lessons Learned

- **Don't use `psycopg2.pool.ThreadedConnectionPool`** — it breaks under Flask debug-mode threading with `PoolError: trying to put unkeyed connection`. `db.py` deliberately opens/closes a fresh connection per request. Neon is serverless; pooling on the client adds nothing.
- **JWT audience/issuer is the origin, not the full base URL.** `aud=https://ep-xxx.neonauth.c-8.us-east-1.aws.neon.tech` (no `/neondb/auth` suffix).
- **EdDSA, not RS256.** Better Auth signs with Ed25519 by default. `auth.py` accepts `["EdDSA", "RS256"]` for safety.
- **The `token` from `/sign-in/email` is a session token, not a JWT.** Must call `/token` separately to get the JWT. See Auth Flow above.
- **`neon_auth."user"` is singular and must be quoted** (SQL reserved word). Earlier code referenced `neon_auth.users_sync` (Stack Auth convention) — wrong.
- **GitHub push protection blocks Stripe-format and Google-API-key patterns** in any committed file. The `.env.example` originally had `sk_live_...` placeholders that triggered it. Hardcoded Spotify/YouTube/Google keys also trigger. Keep credentials in env vars only.
- **Neon Auth allows the Vercel domain via CORS automatically** — no allowlist config needed in the Neon Console for `*.vercel.app`.
- **Cold starts** on Vercel Hobby Python: ~1-2 sec after idle. Subsequent requests are instant.
- **Neon free tier suspends idle compute** — first DB request after long idle takes a couple seconds to wake.
- **Sessions across browser refreshes:** JWT in sessionStorage; if expired, the page-load IIFE silently exchanges the still-valid HttpOnly cookie for a fresh JWT.

## Common Tasks

| Task | How |
|---|---|
| Run locally | `python app.py` |
| Apply schema changes | Edit `schema.sql` → `python init_db.py` |
| Deploy | `git push origin main` |
| Watch Vercel build/runtime logs | Vercel dashboard → Project → Deployments → click latest |
| Reset a user's data | SQL: `DELETE FROM songs WHERE user_id = '...';` (cascades to set_list_songs) |
| Wipe all app data (not auth) | SQL: `TRUNCATE songs, set_lists, set_list_songs, user_tunings CASCADE;` |
| Inspect Neon DB | Neon Console → SQL Editor |

## Conventions

- All `saveState`/`loadState` localStorage logic is GONE. State lives in Postgres; the frontend reads on `loadAppData()` and writes via `apiFetch()` helpers (`apiAddSong`, `apiUpdateSong`, `apiDeleteSong`, `saveSetlistDebounced`, `apiAddTuning`).
- Set list saves are **debounced 500ms** to batch rapid drag-drop reorders into one POST.
- Song mutations are **fire-and-forget** (no await) — optimistic UI; failures show a toast.
- `_jwks_cache` and `_config` globals in Python modules are warm-cache only — fine to reset on cold start.

## Maintenance Pattern for This File

When future sessions do meaningful work in this project, ask Claude:

> "Append a section to CLAUDE.md describing what we did in this session. Use existing sections as a style reference. Don't bloat — only include things future sessions will need to know."

That way this file grows incrementally without you having to write it.
