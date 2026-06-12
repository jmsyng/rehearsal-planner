# Rehearsal Planner — Project Guide for Claude

> **Starting a new session? Read this file top-to-bottom before touching anything else.**
> The stack, file layout, auth flow, gotchas, and recent session notes below will answer
> most questions before you ask them. Skipping this step is what causes false assumptions
> about project structure (e.g. guessing this is a React/TypeScript app — it is not).

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
├── schema.sql          # Canonical DDL — reflects current target state; used by init_db.py (fresh DBs only)
├── init_db.py          # Run once on a FRESH database: applies schema.sql + pre-stamps schema_migrations — do NOT run on existing DBs
├── migrate.py          # Incremental migration runner — safe to run anytime; skips already-applied files
├── migrations/         # Numbered SQL migration files (001_..., 002_...) applied by migrate.py
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
- `songs` — UUID PK `id`; `external_id TEXT` (Spotify track ID, or NULL for custom songs); exactly one of `band_id`/`user_id` set (CHECK constraint); `added_by UUID`; no `plays` column
- `setlists` — UUID PK; `name TEXT DEFAULT 'Main Set'`; `band_id XOR user_id`; multiple per band/user supported
- `setlist_songs` — `(setlist_id, song_id)` composite PK; `position INTEGER`; `plays INTEGER`
- `bands`, `band_members` — band + membership
- `song_proposals`, `song_votes` — voting system
- `notifications`, `notification_prefs` — per-user notification settings
- `user_profiles` — display name + roles (app-side; Neon Auth owns `neon_auth."user"`)
- `user_tunings` — custom tunings beyond the 4 hardcoded defaults
- `schema_migrations` — tracks which migration files have been applied (managed by `migrate.py`)

`user_id` is **UUID** everywhere. **Old tables `set_lists`, `set_list_songs`, `band_set_list_songs` were removed in migration 001** — replaced by `setlists`/`setlist_songs`. Don't reference them.

Duplicate-guard partial unique indexes prevent the same Spotify song being added twice to one library:
- `songs_band_external_uniq` on `(band_id, external_id) WHERE band_id IS NOT NULL AND external_id IS NOT NULL`
- `songs_user_external_uniq` on `(user_id, external_id) WHERE user_id IS NOT NULL AND external_id IS NOT NULL`

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

### Keeping localhost up — run as a launchd service (NOT via the preview tool)

The local server is managed by a **launchd LaunchAgent** so it survives crashes, reboots,
and Claude sessions: `~/Library/LaunchAgents/com.rehearsal-planner.dev.plist`
(`RunAtLoad` + `KeepAlive`, runs `/usr/bin/python3 app.py`, logs to `/tmp/rehearsal-planner.log`).

```bash
UID_NUM=$(id -u); PLIST=~/Library/LaunchAgents/com.rehearsal-planner.dev.plist
launchctl bootstrap gui/$UID_NUM "$PLIST"     # start/install
launchctl bootout   gui/$UID_NUM/com.rehearsal-planner.dev   # stop/uninstall
launchctl kickstart -k gui/$UID_NUM/com.rehearsal-planner.dev # restart (after a .py edit)
launchctl print gui/$UID_NUM/com.rehearsal-planner.dev | grep -E 'state|pid'
tail -f /tmp/rehearsal-planner.log
```

- **Do NOT start the long-lived server with the Claude Preview MCP (`preview_start`).**
  Those servers are child processes of the preview MCP and get reaped when the MCP
  reconnects or the session cycles — that was the recurring "localhost is down again."
  Use `preview_start` only for throwaway screenshot/eval checks; for a server you rely on,
  use the launchd agent (or `( nohup python3 app.py >/tmp/rp.log 2>&1 & )` for a quick detach).
- **launchd owns 5050; the preview server runs on 5051 (they coexist).** `app.py`'s port is
  configurable — `python3 app.py [port]` (CLI arg) or `$PORT`, **defaulting to 5050** so the
  plist and a plain `python3 app.py` are unchanged. `.claude/launch.json` passes `5051`, so
  `preview_start` binds 5051 and never collides with the launchd server on 5050. If you ever
  want `preview_start` to own 5050 instead, `launchctl bootout` the agent first to free the port.
- **`RP_SERVICE=1`** (set in the plist) makes `app.py` run with `use_reloader=False` — one
  process for launchd to supervise cleanly. Plain `python3 app.py` is unchanged (reloader on).
  Because the service has no reloader, **restart it after editing a `.py`** (`kickstart -k`);
  template/JS edits hot-reload and need no restart.

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

- **"localhost keeps going down" = it was started via the Claude Preview MCP (`preview_start`).** Those servers are children of the preview MCP and die when it reconnects / the session cycles. Run the persistent server via the launchd agent instead (see Local Development → Keeping localhost up).
- **A `transform` transition on a draggable element makes it lag behind the finger on touch.** SortableJS drags the touch fallback clone by updating its `transform` every move; if the element has `transition: transform …`, each update animates → "molasses" drag on mobile. Fix: `transition: none` on `.song-card.sortable-drag, .song-card.sortable-fallback`. Keep transitions off anything Sortable repositions live.
- **Neon Auth Trusted Origins must be exact URLs — no wildcards** (the UI rejects `https://*.…` as an invalid URL). Only the production origin (`https://rehearsal-planner.vercel.app`) is trusted, so **sign-in fails with "Invalid origin" on Vercel preview deploys.** To test signed-in features on a preview, add that preview's exact branch-alias origin (e.g. `https://rehearsal-planner-git-<branch>-…-jmsyngs-projects.vercel.app`) to Trusted Origins, then remove it after the branch is merged/deleted.
- **Don't use `psycopg2.pool.ThreadedConnectionPool`** — it breaks under Flask debug-mode threading with `PoolError: trying to put unkeyed connection`. `db.py` deliberately opens/closes a fresh connection per request. Neon is serverless; pooling on the client adds nothing.
- **JWT audience/issuer is the origin, not the full base URL.** `aud=https://ep-xxx.neonauth.c-8.us-east-1.aws.neon.tech` (no `/neondb/auth` suffix).
- **EdDSA, not RS256.** Better Auth signs with Ed25519 by default. `auth.py` accepts `["EdDSA", "RS256"]` for safety.
- **The `token` from `/sign-in/email` is a session token, not a JWT.** Must call `/token` separately to get the JWT. See Auth Flow above.
- **`neon_auth."user"` is singular and must be quoted** (SQL reserved word). Earlier code referenced `neon_auth.users_sync` (Stack Auth convention) — wrong.
- **GitHub push protection blocks Stripe-format and Google-API-key patterns** in any committed file. The `.env.example` originally had `sk_live_...` placeholders that triggered it. Hardcoded Spotify/YouTube/Google keys also trigger. Keep credentials in env vars only.
- **Neon Auth does NOT auto-trust Vercel domains.** You must add `https://rehearsal-planner.vercel.app` to the Trusted Origins list in Neon Console → Auth → Trusted Origins. Without this, sign-in from the live app returns "Invalid origin" (Better Auth rejects the cross-origin request).
- **Cold starts** on Vercel Hobby Python: ~1-2 sec after idle. Subsequent requests are instant.
- **Neon free tier suspends idle compute** — first DB request after long idle takes a couple seconds to wake.
- **Sessions across browser refreshes:** JWT in sessionStorage; if expired, the page-load IIFE silently exchanges the still-valid HttpOnly cookie for a fresh JWT.

## Common Tasks

| Task | How |
|---|---|
| Run locally | `python app.py` |
| Apply schema changes to existing DB | Write `migrations/NNN_description.sql` → `python migrate.py` |
| Create a fresh DB from scratch | `python init_db.py` (NOT for existing DBs) |
| Deploy | `git push origin main` |
| Watch Vercel build/runtime logs | Vercel dashboard → Project → Deployments → click latest |
| Reset a user's data | SQL: `DELETE FROM songs WHERE user_id = '...';` (cascades to setlist_songs) |
| Wipe all app data (not auth) | SQL: `TRUNCATE bands, songs, setlists, user_tunings, user_profiles, notifications CASCADE;` |
| Inspect Neon DB | Neon Console → SQL Editor |

## Conventions

- All `saveState`/`loadState` localStorage logic is GONE. State lives in Postgres; the frontend reads on `loadAppData()` and writes via `apiFetch()` helpers (`apiAddSong`, `apiUpdateSong`, `apiDeleteSong`, `saveSetlistDebounced`, `apiAddTuning`).
- Set list saves are **debounced 500ms** to batch rapid drag-drop reorders into one POST.
- Song mutations are **fire-and-forget** (no await) — optimistic UI; failures show a toast.
- `_jwks_cache` and `_config` globals in Python modules are warm-cache only — fine to reset on cold start.

## Session Notes

### Session: Spotify Typeahead + Auth Planning (2026-05-27)

**Completed:**
- **Spotify search typeahead dropdown** (working feature):
  - Added `keyup` event listener with 500ms debounce for real-time search-as-you-type
  - Results container now hidden by default, only shows when results are available
  - Result items have hover effects (accent-dim background) and compact "Lib"/"Set" buttons
  - Tested end-to-end: search, preview, add to Library, add to Set List — all working
  - Modal closes automatically after selection
  
- **Add Song Modal** (fully functional):
  - Two tabs: "Search Spotify" (working) + "Add Custom Song" (working)
  - Form validation: action buttons appear only when required fields filled
  - Tested custom song creation (name, artist, duration, tuning)
  - All songs persist in localStorage and display correctly
  
- **Song deduplication verified**:
  - Songs in Set List do NOT appear in Library (filter at line 1566 of index.html works)
  - Tested: 37 initial songs, adding 2 via Spotify, adding 1 custom → all counts correct
  - Library: 39 songs (deduped), Set List: 1 song

**Planned (Phase 2, not yet implemented):**
- Neon PostgreSQL database setup (users, auth_tokens, sessions tables)
- Magic link passwordless authentication (no passwords)
- Database module (`db.py`) for Postgres CRUD
- Auth module (`auth.py`) for token generation, email sending, session management
- Update Flask app with `/auth/request-magic-link`, `/auth/verify`, `/auth/logout`, `/api/me` routes
- Login page UI (email input, magic link status)
- Keep localStorage for Phase 1; migrate songs/setlists to DB in Phase 2

**Architecture decision:**
- Staying with vanilla JavaScript + Flask for now (not migrating to React)
- If app complexity grows significantly in Phase 2, React migration can be revisited

**Next steps:**
- Implement Neon + magic link auth in a fresh session (other sessions currently running on this project)
- Once auth is live, migrate app data (songs, setlists) from localStorage to Postgres

### Session: Themed Panels + Mobile Tabs + Dev Bypass (2026-05-27)

**Completed:**

- **Fixed broken Library/Set List search inputs.** Dead JS for a removed header `#spotify-search-input` was calling `.addEventListener` on `null`, throwing a TypeError that halted the rest of that `<script>` block — so the search input listeners (declared later in the same block) never registered. Removed the orphaned listeners. **Lesson:** when removing HTML elements, grep for `getElementById('<id>')` and clean up referenced JS, or guard with `?.addEventListener`.

- **Right-aligned the "+ Add Song" button** in the header (`justify-content: space-between`).

- **Mobile tabbed layout (≤800px).** Added `#mobile-tab-bar` with two raised "paper folder" tabs. Above 800px the two panels sit side-by-side as before; below 800px they stack via `position: absolute; inset: 0` and only the panel matching the active tab gets `.mobile-active { display: flex }`. Switching is handled by `initMobileTabs()` at the end of the second `<script>` IIFE.
  - **CSS cascade gotcha:** the mobile `.panel { display: none }` rule MUST live in a media query that comes AFTER the base `.panel { display: flex }` rule in source order, or it loses to specificity ties. Two media queries near the top of `<style>` handle everything else; the panel display override lives in its own `@media` at the very end of `<style>`.

- **Differentiated themes for Library vs Set List.** Extended `:root` with two cool/warm accent triples (`--library-accent` cyan, `--setlist-accent` amber, each with `-dim` and `-tint` variants). Applied at the end of `<style>` via `#library-panel ...` / `#setlist-panel ...` selectors: panel tint, header bottom-stripe, count badge color, song-card `border-left: 3px solid`, hover, focused search border. Each panel header `h2::before` also gets a distinct glyph (♪ Library, ≣ Set List) so the differentiation works in grayscale. Tabs in the mobile bar adopt the same colors. Scope new themed selectors to the panel IDs, not a class.

- **Dev-mode auth bypass.** On `localhost`/`127.0.0.1`/`0.0.0.0` with no `_accessToken`, an IIFE right after the session-restore one hides `#auth-overlay`, seeds 12 sample songs into `libraryData`, seeds a few `setListIds`, and calls render + `fetchAllAlbumArt()` (the album-art endpoint is public). In addition, every `api*` helper (`apiAddSong`, `apiUpdateSong`, `apiDeleteSong`, `apiSaveSetlist`, `apiAddTuning`) short-circuits with `if (!_accessToken) return;` (or `return song` for the additive one). Without the short-circuits, dev-mode silently failed on every mutation because the backend returned 401 and `addCustomSong` (et al) early-returned on `!saved`. **Production is unchanged** — when a token exists, the bypass IIFE skips and helpers hit the backend normally.

- **Spotify search results in Add Song modal — display fix.** `performAddSongSpotifySearch` populated `#add-song-spotify-results` but never set its inline `display: none` to `flex`. Added one line at top of the function: `addSongSpotifyResults.style.display = 'flex'`.

- **Themed Spotify-result add buttons.** Per-row "+ Library" / "+ Set List" buttons now use destination panel colors (`var(--library-accent-dim)` / `var(--setlist-accent-dim)`) with matching hover. Footer "+ Library" / "+ Set List" buttons (for Add Custom Song tab) also themed.

- **Footer Add buttons — visibility rules.** `#add-song-actions` is now only shown on the Custom Song tab. Removed: (1) a duplicate `display:flex` in the inline style that made it always visible regardless of `display:none`, (2) the input-field listener that toggled visibility based on required-field content. Tab-switch handler now drives visibility: `addSongActions.style.display = tabName === 'custom' ? 'flex' : 'none'`. Required-field validation still happens inside `addCustomSong()` itself, so users get the alert if they click the button on an incomplete form.

- **New songs land at top of library.** Changed `libraryData.push(saved)` → `libraryData.unshift(saved)` in all four add paths (custom song + Spotify song, each × library/setlist destination). User immediately sees what they just added.

- **Auto-scroll expanded card into view.** Both `renderLibrary()` and `renderSetList()` now call `expandedCard.scrollIntoView({ block: 'nearest', behavior: 'smooth' })` after rendering when `expandedSongId` is set. Fixes the "card disappeared" symptom on mobile where expansion pushed the expanded section below the viewport.

### Session: Trusted Origins + Tuning Save Fix (2026-05-28)

**Completed:**

- **Fixed "Invalid origin" error on live Vercel app.** Sign-in from any real browser (phone, desktop) was returning "Invalid origin" from Neon Auth (Better Auth). Root cause: Better Auth requires the client app's origin to be explicitly listed as a trusted origin — it does NOT auto-trust `*.vercel.app` domains. Fix: added `https://rehearsal-planner.vercel.app` to Trusted Origins in Neon Console → Auth. No code change needed. Updated Gotchas note (prior session had incorrectly noted it was automatic).

- **Fixed tuning changes not persisting.** The Save button handler in the expanded song card referenced three undefined variables (`recordedInput`, `ourInput`, `youtubeInput`), causing a `ReferenceError` before `apiUpdateSong` was ever called. The tuning `<select>` dropdowns already update `song.extra.RecordedTuning` / `song.extra.OurTuning` directly via their `change` event handlers, so the save button only needs to call `apiUpdateSong(song)`. Removed the three broken lines.

- **Fixed tuning badge not reflecting saved value.** The chip on the song card read `song.extra?.Tuning` but the field saved by the tuning dropdown is `song.extra.OurTuning`. Changed to `song.extra?.OurTuning || song.extra?.Tuning` (fallback keeps legacy songs working).

- **Noted:** The dev bypass (localhost auth skip) described in the previous session's notes is NOT present in the current `index.html`. Either it was never committed or was removed. Don't assume it exists.

### Session: Band Collaboration Feature (2026-05-28)

**Completed:**

- **New database tables:** `bands`, `band_members`, `song_proposals`, `song_votes`, `notifications`, `band_set_list_songs`. Also added `band_id UUID` column to `songs` via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` (safe to re-run). Use `gen_random_uuid()::text` for invite tokens — `gen_random_bytes` requires `pgcrypto` which isn't enabled on Neon free tier.

- **Band-aware routing in `app.py`:** All four key routes (`GET/POST /api/songs`, `GET/POST /api/setlist`) now call `db.get_user_band(g.user_id)` at the top and branch to band-scoped DB functions if the user is in a band, else fall back to existing per-user behavior. Solo users are completely unaffected.

- **New API routes:** `POST /api/band`, `GET /api/band`, `POST /api/band/join`, `GET /api/band/proposals`, `POST /api/band/vote`, `GET /api/notifications`, `POST /api/notifications/read`.

- **Scoring model:** Yay=2, Meh=1, Boo=0. Threshold ≥5 to approve (max 8 for 4-member band). Proposer auto-votes Yay (score starts at 2). Evaluated eagerly after each vote. On approval: song status→`Learning`, auto-added to `band_set_list_songs`. On rejection: song status→`Resting`.

- **Migration on join:** `db.migrate_songs_to_band()` runs when admin creates band or member joins — sets `band_id` on all their personal songs and copies their personal setlist into `band_set_list_songs`.

- **Frontend:** Invite link join via `?join=TOKEN` URL param (stored in `sessionStorage`). Band setup screen shown to users without a band. Voting modal with Yay/Meh/Boo buttons and vote-tally chips. Notification bell (🔔) with red badge in header, polls every 60s. Song cards show "Added by [name]" and `⚡ N/8` score badge for pending proposals. Band info modal with member list + copy-invite-link button.

- **`_bandData` global** in frontend holds `{id, name, invite_token, role, members:[...]}` — used by `makeSongCard` to conditionally show band metadata. Bell/band-info buttons are hidden for solo users.

- **`init_db.py` is idempotent** — safe to run on existing databases. All new tables use `CREATE TABLE IF NOT EXISTS`; the column addition uses `ADD COLUMN IF NOT EXISTS`.

**Testing the band feature:**

- **The invite link has no "uses" and never expires.** It's a permanent token (`bands.invite_token`, a UUID). The only real constraint is the **4-member cap** enforced in `db.join_band()` — testing with throwaway accounts consumes real seats against that cap.
- **A band must exist before any invite link can be generated.** Sign in → band setup screen → create band → click the **🎸 Band** header button to copy the link. There's no band in the DB until someone creates one through the UI (or via `POST /api/band`).
- **To test the join/vote flow:** use incognito windows with throwaway emails, visit the `?join=<token>` URL, then reclaim seats afterward.
- **Reset test members** (keeps you as admin, frees the other 3 slots):
  ```sql
  DELETE FROM band_members
  WHERE band_id = (SELECT id FROM bands LIMIT 1)
    AND role != 'admin';
  ```
- **Inspect current band membership:**
  ```sql
  SELECT bm.user_id, nu.email, bm.role, bm.joined_at
  FROM band_members bm
  JOIN neon_auth."user" nu ON nu.id = bm.user_id;
  ```
- **Wipe all band data** (bands, members, proposals, votes, notifications, shared setlists — cascades from `bands`):
  ```sql
  TRUNCATE bands CASCADE;
  ```
  Note: this leaves `songs.band_id` dangling as NULL (FK is `ON DELETE SET NULL`), so songs revert to per-user ownership.

### Session: Song Card UI Redesign (2026-05-30)

**Status: UNCOMMITTED at session end.** All changes (this session's card redesign in `index.html` PLUS the prior Band Collaboration backend in `app.py`/`db.py`/`schema.sql`) are still uncommitted and NOT deployed. The live Vercel app still runs the old UI. Suggested commit split: (1) band backend, (2) card redesign.

**Completed (all in `templates/index.html`, function `makeSongCard` + `<style>`):**

- **Two-row card layout.** Cards were one cramped horizontal row stuffing 8+ elements. Now split into two rows:
  - **Top row (`.song-card-row`)**: album art (now 56×56, was 48×48) + a 3-line info column — line 1 Artist, line 2 Song title with inline `[tuning chip]`, line 3 Duration with inline `[status badge]`. This restores the original Artist/Title/Duration stacking the user preferred (an intermediate single-meta-line version was rejected).
  - **Bottom row (`.song-card-actions`)**: drag handle (`⠿`) on the far left, then contextual meta (Added-by, playthrus, vote buttons, score), a flex spacer, then action buttons (`+ Add`/`❯` expand/`×` remove) pushed right.

- **`makeSongCard` was made `flex-direction: column`** on `.song-card` (was `row`). The drag handle moved OUT of the content row INTO the actions row — still a `.drag-handle` element so SortableJS is unaffected.

- **Status badges** (`.status-badge`, new): colored pill on the duration line. `LEARNING` green, `PROPOSED` amber, `RESTING` red, `ACTIVE` purple. Driven by `song.extra.proposalStatus` (band: pending→Proposed, approved→Learning, rejected→Resting) OR `song.extra.songStatus` (solo manual). CSS class is just the lowercased label.

- **Solo status field in expanded card.** When `!_bandData`, the expanded card shows a Status `<select>` (Active/Learning/Proposed/Resting/Shelved) writing to `song.extra.songStatus`. Band users don't get it — their status comes from voting. NOTE: this only sets the field in memory; the existing Save button calls `apiUpdateSong(song)` to persist.

- **Inline vote buttons** (`.vote-control`/`.vote-btn`, new): band pending proposals get `[Yay][Meh][Boo]` directly on the card (previously voting was modal-only). Active vote is highlighted via `.vote-btn.vote-{yay,meh,boo}.active`.

- **BUG FIX — inline vote was sending wrong field.** First pass sent `{song_id}` but `POST /api/band/vote` requires `{proposal_id}` (see `app.py:259`). This caused the request to 400, and an unhandled path kicked the user to the auth screen. Fixed to read `song.extra.proposalId` (already provided by `db.py:157`). Vote buttons now only render when `proposalId` is present. Handler mirrors the existing modal `submitVote`: optimistic re-render, then on `approved`/`rejected` result reloads `/api/songs` + `/api/setlist`.

- **Enthusiasm score badge** (`.score-badge`, new): `⚡ N/8` moved into the actions row meta for band pending proposals.

- **Verification done** via Claude Preview MCP (localhost:5050) with injected mock `libraryData`/`_bandData` — the dev-mode injection is manual via `preview_eval`, NOT a code bypass (no localhost auth-skip exists in `index.html`, per prior session note). Confirmed mobile + desktop side-by-side, no console errors.

### Session: Likert Voting, Library Integration & Ongoing Ratings (2026-05-30)

**Completed:**

- **5-point Likert scale replaces Yay/Meh/Boo.** Votes stored as `"1"`–`"5"` (TEXT). `VOTE_POINTS = {"5":5,"4":4,"3":3,"2":2,"1":1}`. Approval threshold is `math.ceil(band_size * 3.5)` — computed dynamically in `cast_vote`, no longer a module constant. Proposer auto-votes `"5"` (was `"yay"`), initial score = 5. Max-possible rejection ceiling now uses `remaining * 5` (was `* 2`). Added `import math` to `db.py`.

- **Voting on ALL approved songs, not just pending proposals.** `cast_vote` now accepts `proposal_id` for proposals with `status IN ('pending', 'approved')`. If an already-approved song's score can no longer reach the threshold, it is set to `status='archived'` and `songs.status='Archived'`.

- **"Archived" replaces "Resting".** Failed/demoted songs get `status='Archived'` (was `'Resting'`). Hidden from Library view by default (`renderLibrary` filters `extra.Status !== 'Archived'`). Archive view is a future addition.

- **Optional reason for low votes.** `song_votes.reason TEXT` column added. Clicking 👎 (2) or 🚫 (1) shows an inline textarea (max 140 chars, not required) before the vote is submitted. Both the card vote buttons and the voting modal prompt for a reason.

- **Failure/demotion notifications.** `notifications.details JSONB` column added. When a proposal is rejected or an approved song is demoted, `_notify_failure()` inserts a notification for every band member with a `details` blob: `{score, max_score, votes:[{name,vote,reason}]}`. Notification types: `"proposal_failed"` and `"song_archived"`.

- **`get_band_songs` enriched.** Now takes `user_id` parameter. Query JOINs `song_proposals` on `status IN ('pending', 'approved')` (was just `'pending'`), LEFT JOINs `song_votes sv_me` for the current user's vote/reason, and runs a `json_agg` subquery for all votes with names and reasons. `_row_to_band_song()` now sets `extra.userVote`, `extra.userVoteReason`, `extra.proposalVotes`.

- **`api_add_song` returns enriched data immediately.** After `create_proposal`, re-fetches via `get_band_songs` and returns the matching enriched song as the 201 response. `libraryData.unshift(saved)` therefore includes `proposalId`, `proposalStatus`, and vote buttons from the moment the song is added.

- **Backfill migration in `init_db.py`.** Four-step idempotent migration appended: (1) remap `yay→5, meh→3, boo→1`; (2) recompute proposal scores; (3) rename `Resting→Archived` on songs; (4) auto-create `status='approved'` proposals for band songs that have none (enables vote-on-all without a null `proposalId`).

- **Library sort.** Pending proposals float to top; unvoted pending first. All other songs preserve API order.

- **`needs-vote` highlight.** Cards where `proposalId` is set and `!userVote` get class `needs-vote`: full-card accent glow border + `::after` pulsing dot on the album art corner.

- **Bell badge driven by unvoted count.** `countUnvotedProposals()` counts `libraryData` entries with a `proposalId` and no `userVote`. `pollNotifications()` now refreshes `/api/songs` (not `/api/notifications`) every 60 s, updates `libraryData`, re-renders, and calls `updateNotifBadge(countUnvotedProposals())`.

- **Score badge format.** `⚡ N/max` where `max = _bandData.members.length * 5` (was hardcoded `/8`). Badge shown for both pending and approved proposals.

- **Expanded card proposal block.** When `proposalId` is set, a proposal-details panel appears at the top of the expanded section: Spotify preview link (if `extra.spotifyUrl`), "Band ratings" chips (name + emoji + truncated reason), and score line `Score: N/max · need X to approve`.

- **Voting modal updated.** Yay/Meh/Boo replaced with 5 Likert buttons (`❤️ Love it` through `🚫 Hard no`). Vote chips use Likert colours. Score line uses new format. Reason textarea appears for 1/2 votes (replaces buttons; has Back option).

- **`doCardVote` refresh logic fixed.** Only does a full `/api/songs`+`/api/setlist` refresh when the song actually disappears from library (rejected or archived) or is newly approved from pending. For an approved song that stays approved, score is patched on the in-memory object and only `renderLibrary()`/`renderSetList()` is called — avoids clobbering unrelated setlist data mid-session.

- **`renderVotingCard` bug fixed** (discovered earlier): card and buttons were not un-hidden after showing the "All caught up" empty state. Added explicit `cardEl.style.display='block'; btnsEl.style.display='flex'; emptyEl.style.display='none'` at the top of the non-empty branch.

**Files changed:** `schema.sql`, `init_db.py`, `db.py`, `app.py`, `templates/index.html`.

**State at session end:** All changes local only (not committed, not deployed). Run `python3 init_db.py` before any deploy to apply migrations.

### Session: Song Card Button Refinements (2026-05-30)

**Completed (all in `templates/index.html`):**

- **`+ Set` button (library panel).** Renamed from `+ Add`; styled orange (`var(--setlist-accent-dim)` bg, `var(--setlist-accent)` border) to signal the destination is the Set List. Disabled/`✓ Set` state handled via CSS `:disabled` (removed inline `opacity` override). Class changed from `ghost add-btn` to `add-btn`.

- **Removed teal `❌ Set` button from collapsed set list cards.** Collapsed setlist cards now show only the drag handle, play-count stepper, and expand chevron — no remove button.

- **Expanded card `❌ Remove from Set` button (set list panel).** In the expanded actions row, the button is now context-aware: when `inSetList`, it shows `❌ Remove from Set` in neutral ghost style and only removes from the setlist (no `apiDeleteSong`). When in the library, it remains `Remove from Library` (red, deletes permanently). This replaces the single always-red `removeLibraryBtn`.

- **"Added by", vote buttons, and score badge moved to expanded view.** These three elements were removed from the collapsed `actionsRow` entirely. They now appear only when the card is expanded:
  - "Added by [name]" renders as a dim `<p>` at the top of `expandedSection`.
  - The full Likert vote control (`voteControl`, `reasonWrap`, `doCardVote`) is constructed inside the proposal context block (`propBlock`) within the `isExpanded` branch, just before the score line.
  - The `⚡ N/max` score badge was removed; the score line (`Score: N/max · need X to approve`) in `propBlock` is sufficient.
  - `needs-vote` card highlight still works — `hasVotableProposal` boolean is computed outside the `isExpanded` block and used for `card.classList.add('needs-vote')`.

- **CSS additions:** `.add-btn` (orange, solid), `.set-remove-btn` (teal — now unused but harmless), hover/disabled states for both.

**State at session end:** All changes local only (not committed, not deployed).

### Session: Needs-Vote Notification Dot Polish (2026-05-30)

**Completed (all CSS in `templates/index.html`, the `.song-card.needs-vote` block ~line 352):**

- **Dot is now red, not accent-purple.** `background: var(--accent)` → `var(--red)` (renders `rgb(240,82,82)`). Signals "action required" rather than the generic accent.

- **Dot moved from top-right to top-left of the album art.** Was `top: 3px; right: 3px`; now `top: 0; left: 0`. (An intermediate `-4px/-4px` centered it exactly on the corner with a full half-overhang; the user then asked to nudge it down-and-right, landing on `0/0` so it tucks into the corner overhanging a bit less.)

- **CLIPPING FIX — the dot was attached to the wrong element.** It lived on `.album-art::after`, but `.album-art` has `overflow: hidden` (needed to clip the cover image to its rounded corners), which cut off the half of the dot that overhangs. Moved the pseudo-element onto the album art's parent `.song-card-row` instead (`.song-card.needs-vote .song-card-row::after`, with `position: relative` on the row). The album art is the row's first child flush at the top-left, so the dot lands in the same spot but is no longer clipped. **Lesson:** a corner badge that overhangs cannot live on an `overflow: hidden` element — put it on a non-clipping ancestor.

- **`@keyframes pulse-dot` rhythm reworked twice.** Final state: duration `2.4s` (was `1.5s`), keyframes
  ```css
  0%, 55% { opacity: 1;   transform: scale(1); }   /* long rest, small + opaque */
  78%     { opacity: 0.5; transform: scale(1.35); } /* the "breath": expand + fade */
  100%    { opacity: 1;   transform: scale(1); }    /* settle back */
  ```
  The dot holds its small, fully-opaque resting state for the first ~55% of each cycle, then takes one slow "breath" out and eases back — instead of pulsing continuously/symmetrically. Easing stays `ease-in-out` for continuous velocity at the endpoints.

**Verification:** Claude Preview MCP (localhost:5050, logged-in Test Band session with 31 live `needs-vote` cards). Confirmed via computed `::after` styles + zoomed screenshots that the dot is red, sits on the album-art top-left corner overhanging onto the card, and is no longer clipped. Animation feel can't be judged from a still — verified only that the keyframes/duration apply with no console errors.

**State at session end:** All changes local only (not committed, not deployed). Still stacked on top of the earlier uncommitted Band Collaboration backend + card redesign + Likert voting work.

### Session: Voting Bug Fixes + Reason Removal (2026-05-30)

Two-part session: fixed six voting-UI bugs, then ripped out the vote-reason feature entirely.

**Part 1 — Six bug fixes (`db.py` + `templates/index.html`):**

- **Bell badge mismatch (badge showed N, modal said "all caught up").** Two causes. (1) `get_pending_proposals` (`db.py`) queried `sp.status = 'pending'` only — the voting modal feeds off this, but the badge counts `pending`+`approved`. Changed to `status IN ('pending', 'approved')`. (2) The real culprit: the backfill migration auto-creates `approved` proposals owned by the user with NO `song_votes` row, so `userVote` is null and the badge counted the user's *own* proposals — but the modal correctly excludes own proposals (`proposed_by != user_id`). Fix: `get_band_songs` now returns `sp.proposed_by::text AS proposer_id` → `_row_to_band_song` sets `extra.proposedBy` → `countUnvotedProposals()` excludes `s.extra?.proposedBy === _currentUserId`. **Lesson:** the proposer auto-vote-5 from `create_proposal` lands in `song_votes`, but migration-backfilled proposals don't have it, so never rely on `userVote` alone to mean "this isn't mine."

- **Expanded card collapsed when clicking vote emoji / Band Ratings area.** The card's expand/collapse click handler (in `makeSongCard`) collapsed on any click not matching `.drag-handle`/`button`/`select`. Non-interactive pixels inside the expanded section bubbled up and collapsed it. Fix: added `e.target.closest('.expanded-info')` to the ignore list. The chevron and Close button manage `expandedSongId` via their own `stopPropagation` handlers and sit *outside* `.expanded-info`, so they still collapse correctly.

- **Score-highlight colours.** `.vote-btn.lk-2.active` was orange → now red (`#f05252`, same as lk-1). `.vote-btn.lk-3.active` (Neutral) grey was too low-contrast → added panel-scoped overrides `#library-panel .vote-btn.lk-3.active` (cyan, `--library-accent`) and `#setlist-panel ...` (amber, `--setlist-accent`) so Neutral matches the song's current location. **Gotcha:** verifying `.active` colours via `preview_eval` by toggling `classList` is unreliable (style recalc didn't apply) — read computed styles off a button that's *natively rendered* with `.active` (e.g. a song the user already voted on).

**Part 2 — Removed the reason feature entirely** (user: "Remove Reason field and functionality from voting. We can discuss like adults IRL."):

- All frontend reason UI gone from `templates/index.html`: the inline `<input>` + Submit/Cancel in the expanded card vote control, and the modal's "Submit vote / Back" reason prompt. Votes 1 & 2 (👎 Pass, 🚫 Hard no) now submit in one click like 3–5. `doCardVote(v)` and `submitVote(proposalId, vote)` no longer take/send a `reason`. Rating chips (card "Band ratings:" + modal "Votes so far:") now render emoji + name only.
- Deleted the `_cardVoteInProgress` flag and the `pollNotifications` render-guard that used it — those were added during Part 1 solely to stop the 60s poll from wiping the open reason field, now dead.
- **Backend left untouched on purpose.** `song_votes.reason` column, `cast_vote(reason=...)`, the `reason` in `_notify_failure` details, and `_row_to_band_song` setting `extra.userVoteReason` all still exist — non-destructive, keeps historical reasons in the DB. The frontend just never sends or displays one now. `grep -n "reason" templates/index.html` returns nothing.

**Verification:** Claude Preview MCP (localhost:5050, Test Band). Confirmed badge→modal now consistent (both 0), vote 2 submits instantly with no reason field and card stays expanded, modal renders all 5 vote buttons with zero reason inputs, chips show emoji+name only, no console errors.

**State at session end:** All changes local only (not committed, not deployed). Still stacked on the earlier uncommitted band/redesign/Likert work.

### Session: User & Band Management UX (2026-05-30)

Built dedicated Band Settings + My Settings experiences, introduced a per-user profile (display name + band roles/instruments) and real notification preferences, and consolidated the header into a ☰ hamburger dropdown. Plan file: `~/.claude/plans/snappy-greeting-grove.md`.

**Schema (`schema.sql`, applied via `python3 init_db.py`):**

- `user_profiles (user_id PK, display_name TEXT, roles TEXT[] DEFAULT '{}', updated_at)` — app-side identity; Neon Auth still owns `neon_auth."user"`. `roles` is profile-display only (no functional gating).
- `notification_prefs (user_id PK, new_proposal, proposal_failed, song_archived BOOLEAN DEFAULT true)` — a **missing row means all-on**, handled via `COALESCE(np.col, true)`, so no backfill needed.
- `ALTER TABLE bands ADD COLUMN approval_factor NUMERIC DEFAULT 3.5` — per-member avg rating to approve; `threshold = ceil(band_size * approval_factor)`. Default 3.5 preserves the old hardcoded behavior. All additive + `IF NOT EXISTS`, so `schema.sql` stays re-runnable (init_db.py executes it wholesale — no edit needed there).

**`db.py`:**

- New: `get_profile`, `upsert_profile`, `update_notif_prefs`, `rename_band`, `set_approval_factor` (clamps 1–5), `regenerate_invite` (`gen_random_uuid()::text`), `promote_member`, `remove_member` (refuses to remove the last admin), `leave_band`, `delete_band`.
- `leave_band` logic: if last member → delete the band (cascades); if sole admin with members remaining → raise `ValueError` (route → 409); else drop the membership row.
- **Display-name propagation:** every place a member/proposer/voter name came from `nu.name` now `LEFT JOIN user_profiles up` + `COALESCE(up.display_name, nu.name, nu.email)`. Touched `get_user_band` (member list, + now returns `roles` and `approval_factor`), `get_band_songs` (proposer_name + the `proposal_votes` json_agg, via `up2`), `get_pending_proposals` (proposer + votes), `_notify_failure` (votes_detail).
- **Notification-pref enforcement:** the two bulk `INSERT ... SELECT FROM band_members` notification writes now `LEFT JOIN notification_prefs np` and filter. `create_proposal` uses `AND COALESCE(np.new_proposal, true)`. `_notify_failure` can't parameterize a column name, so it uses `COALESCE(CASE %s WHEN 'proposal_failed' THEN np.proposal_failed WHEN 'song_archived' THEN np.song_archived END, true)` with `notif_type` passed twice.
- `cast_vote` threshold: fetches `b.approval_factor` in the proposal-lookup JOIN and uses `ceil(band_size * approval_factor)` instead of the literal 3.5.

**`app.py` (all `@require_auth`):** `GET/PUT /api/profile`, `PUT /api/profile/notifications`, `PATCH /api/band` (`{name?, approval_factor?}`, any member), `POST /api/band/invite/regenerate`, `POST /api/band/leave` (409 on sole-admin block), `PATCH /api/band/members/<id>` (promote, admin-only), `DELETE /api/band/members/<id>` (admin-only), `DELETE /api/band` (admin-only). Admin checks read `db.get_user_band(g.user_id)["role"]`.

**Permissions model:** flat — any member can rename / regenerate invite / change threshold. **Admin-only:** remove member, promote member, delete band. Multiple admins allowed (promote enables admin succession before a sole admin leaves).

**Frontend (`templates/index.html`):**

- **Header** trimmed to title, **+ Add Song**, 🔔 bell, 🎸 band-name button (now opens Band Settings), and a new **☰** `#menu-btn`. The old `#user-email` span + Sign Out button were **removed** — identity + Sign Out now live in the `#app-menu` dropdown. (Anything that referenced `#user-email` had to change — `onAuthSuccess` now stores `_userEmail` and calls `updateMenuIdentity()`.)
- New globals: `_userProfile` (`{display_name, roles, notif_prefs}`, loaded in `loadBandAndAppData`, reset in `signOut`), `_userEmail`, `ROLE_OPTIONS`. `_profileRoles` holds the in-flight role selection for the profile editor.
- `#app-menu` dropdown: Band Settings / My Settings / identity / Sign Out; toggled by `toggleAppMenu`, closed via a document click-outside listener + Escape (`closeAppMenu`).
- **Band Settings modal** reuses the old `#band-info-modal` id (so `signOut` etc. still find it) but is fully rebuilt: rename (`saveBandName`), members list (`renderBandMembers` — shows display name + instrument `.tag-pill`s + role badge; admin sees Make admin / Remove on non-admin rows, never on self), invite copy + `regenerateInvite`, an `approval-factor` range slider with a live helper (`updateApprovalHelp`), and an admin-only danger zone (`deleteBand`). `leaveBand`/`deleteBand` call `resetToBandSetup()` which clears state and re-shows `#band-setup-overlay`.
- **My Settings modal** (`#user-settings-modal`, new): display name + role chips (`renderProfileRoles`/`toggleRole`/`addCustomRole`) → `saveProfile` (PUT, then refreshes `/api/band` + `/api/songs` so the new name shows on members and song authors); tunings add/view (`renderSettingsTunings`/`addTuningFromSettings`, reuses `POST /api/tunings` — **removal deliberately deferred**, no DELETE endpoint); notification toggles (`saveNotifPrefs`, fires on change); account actions (Sign Out / Leave band).
- New CSS block before `<header>`: `.app-menu-item`, `.settings-*`, `.role-chip`, `.tag-pill`, `.toggle-row` + `.switch` (CSS toggle). Added an `escapeHtml` helper (member/role strings are user-supplied).

**Verification:** Claude Preview MCP (localhost:5050, injected mock logged-in state — no localhost auth bypass exists, same as prior sessions). Confirmed header/menu, Band Settings (admin + non-admin gating), My Settings all render with no console errors; role toggles + custom roles + slider math (3.5→11/15, 4→12/15) correct; notif toggles bind from profile. **Backend smoke-tested against the live DB**: `get_user_band` returns `approval_factor` + member roles, display-name COALESCE joins resolve (`get_band_songs` proposer), profile + notif-pref round-trips persist and surface in the member list (then restored). Both notification-filter INSERTs validated in a rolled-back transaction.

**State at session end:** All changes local only (not committed, not deployed). `python3 init_db.py` already run locally; must run against the target DB before deploy.

### Session: Local Dev Sign-In Fix (2026-05-30)

**Problem:** Sign-in on `http://localhost` silently never completes ("nothing happens"). Root cause is environmental, NOT a code bug — the browser-side flow works perfectly on HTTPS/production. Neon Auth mints the JWT only after setting `__Secure-neon-auth.session_token` (`SameSite=None; Secure`, owned by the `*.neon.tech` origin). Relative to `localhost` that's a **cross-site cookie**, which modern browsers block by default, so `sign-in/email` returns 200 but the cookie is dropped and the follow-up `/token` exchange gets 401. Verified `/token` *requires* that cookie — passing the body session token as `Authorization: Bearer …` (no cookie) still 401s. **This is why every prior session tested by injecting mock logged-in state instead of really signing in.**

**Fix — localhost-only server-side auth proxy** (the cookie round-trip works from Python, which isn't bound by browser cross-site rules):

- **`app.py`:** added `POST /api/dev/login` and `POST /api/dev/token`, plus helpers `_is_local_dev()`, `_neon_session_cookie()`, `_exchange_session_for_jwt(base, cookie_header, origin)`. Added `import urllib.error`.
  - `/api/dev/login` (`{mode:'signin'|'signup', email, password, name?}`) calls Neon Auth `sign-in|sign-up/email` server-side, captures `Set-Cookie`, replays it to `/token`, returns `{token, email, devSession}` where `devSession` is the collapsed cookie header.
  - `/api/dev/token` (`{devSession}`) replays the stored cookie to `/token` for the 15-min JWT refresh.
  - **Gotcha: Better Auth rejects originless requests** ("Origin header is required when callbackURL is not an absolute URL"). The browser sends `Origin` automatically; the server-side `urllib` call must add `Origin: <request.host_url>` (the app's own origin, which is already a trusted origin since browser sign-up from it works). Added to both the sign-in/up and the `/token` requests.
  - **Both routes are localhost-gated:** `_is_local_dev()` returns false if `os.environ["VERCEL"]` is set or `request.remote_addr` isn't loopback (`127.0.0.1`/`::1`) → 404. Cannot be used in production.
- **`templates/index.html`:** added `const IS_LOCAL_DEV = ['localhost','127.0.0.1','0.0.0.0'].includes(location.hostname)`. When true: `neonSignIn`/`neonSignUp` delegate to new `devAuth(mode, email, password)` (POSTs `/api/dev/login`, stores `rp_access_token` + `rp_dev_session`, calls `onAuthSuccess`); `fetchNeonJwt` refreshes via `/api/dev/token` using stored `rp_dev_session`. `signOut` now also clears `rp_dev_session`. **Production path is byte-for-byte unchanged** — the `IS_LOCAL_DEV` branch never runs off localhost.

**So: real local sign-in now works against the real DB** (no more mock-state injection needed). `devtest_signin@example.com` / `DevTest!2345` exists as a usable local test account; a couple of `diag…@example.com` throwaways were also created in the Neon Auth pool during diagnosis (safe to delete).

**Verified** (Claude Preview, localhost:5050): UI sign-up → `/api/dev/login` 200 → authed `/api/band`+`/api/profile` 200; JWT refresh via `/api/dev/token`; session restores on reload; no console errors.

**State at session end:** Local only (not committed, not deployed). Stacked on the earlier uncommitted work. Also in this session: moved the `needs-vote` dot up/left to `top:-2px;left:-2px` and removed the `.song-card.needs-vote` border/box-shadow highlight (dot is now the sole indicator).

### Session: Reddit-style Header Redesign (2026-05-30)

Rebuilt the app header to mirror the Reddit mobile layout, plus search consolidation and inline-style cleanup. **All in `templates/index.html`.** Plan file: `~/.claude/plans/breezy-seeking-lagoon.md`.

**Layout (markup `~1779`, replacing the old single-row header):** `<header>` is now a `flex-direction:column` sticky bar with two rows:
- **Top row** (`.hdr-top`): left = `#band-menu-btn` (☰) + `.hdr-logo`; right = `#user-info` (`.hdr-right`) holding `#add-song-btn`, `#notif-bell-btn`, and `.hdr-user-wrap` (avatar + `#user-menu`).
- **Search row** (`#global-search-row`, `.hdr-search`): one full-width bar.

**Two menus replace the single `☰`/`#app-menu`.** Left `#band-menu` = band-name label (still `#band-name-display`) + ⚙️ Band Settings. Right `#user-menu` (under the avatar) = identity (`#menu-identity-name`/`-email`) + 👤 My Settings + ↩ Sign Out. The old standalone 🎸 `#band-info-btn` is **gone** — folded into the left menu. JS: `toggleAppMenu`/`closeAppMenu` replaced by `toggleBandMenu`/`toggleUserMenu`/`closeAllMenus` (opening one closes the other; outside-click via `e.target.closest(...)` + Escape close both). `closeAppMenu()` kept as a back-comat alias (still called in `signOut`).

**Avatar** (`#user-avatar-btn`, `.hdr-avatar`): first initial of `display_name || email` in a solid circle. Color is **deterministic per user** via `avatarColorFor(seed)` (charcode hash → one of `--avatar-0..5`, new tokens in `:root`). Set in `updateMenuIdentity()` alongside the initial. Per-user color customization is future work.

**Global hybrid search replaces the two per-panel search boxes.** The old `#library-search`/`#setlist-search` inputs + their listeners + their CSS were **removed**. New `#global-search` listener calls `applyGlobalSearch(v)` which sets **both** `librarySearchText` and `setListSearchText` and re-renders both panels — the existing filter logic inside `renderLibrary`/`renderSetList` is unchanged, so both panels filter from one bar. **Spotify fallback:** `renderLibrary`'s no-results branch, when a query is present, appends a `.spotify-fallback-btn` ("🔎 Search Spotify for '<q>'") wired to new `openAddSongWithQuery(q)` — opens `#add-song-modal`, clicks the Spotify tab, prefills `#add-song-spotify-input`, runs existing `performAddSongSpotifySearch()`.

**Visibility helper:** added `setBandChrome(hasBand)` toggling `#band-menu-btn` + `#notif-bell-btn` + `#global-search-row` together; replaced the 4 old `band-info-btn`/`notif-bell-btn` show/hide sites (auth load, create-band, join-band, `resetToBandSetup`) with it. Solo (no band) users see none of the three (band-setup overlay covers the screen anyway); the avatar menu is always available when authed.

**Verification** (Claude Preview, localhost:5050, real sign-in as `devtest_signin@example.com` then **mock band/song state injected** because that account has no band — bare-name assignment, e.g. `_bandData = {...}`, not `window._bandData`, since the app's state vars are top-level `let` bindings): confirmed desktop (1280px) + mobile (375px) layout, add-button collapses to `＋` icon ≤800px, global search filters both panels with `N of M` counts, Spotify fallback opens the modal prefilled (10 results), both menus toggle/mutual-exclude/outside-click/Escape, avatar initial + stable color, Band/My Settings modals open, no console errors. **Note:** `preview_click` on the dynamically-rendered fallback button didn't fire the handler (synthetic-event/scroll quirk); `.click()` and a direct call both worked — not a code bug.

**State at session end:** Local only (not committed, not deployed). Stacked on all the earlier uncommitted work (band backend, card redesign, Likert voting, settings UX, dev sign-in).

### Session: Ship the backlog + mobile drag fix (2026-05-30)

Committed and deployed the long-stacked uncommitted work, then ran a first full branch→PR→preview→merge cycle for a bug fix.

- **Committed the whole backlog** in 3 layer commits (backend / frontend `templates/index.html` / docs) on a branch, merged to `main`, pushed → Vercel auto-deployed. **Ran `init_db.py` against prod first** so the band-aware routes (`get_user_band` on `/api/songs`, `/api/setlist`) didn't 500 on a missing schema. Verified via Vercel runtime logs (all DB-backed routes 200).
- **Mobile Set List drag fix** (`fix-mobile-setlist-drag-lag`, merged PR #1): removed the `transform` transition from the dragged element — see the SortableJS gotcha above. Verified the *feel* on a real phone via the Vercel **preview deploy** before merging.
- **Process notes for future changes:** real changes go through a branch → push (auto preview) → test the preview → PR → merge (= prod deploy) → delete branch. Docs-only edits like this one can go straight to `main` (no runtime risk). Testing signed-in features on a preview requires adding that preview's origin to Neon Trusted Origins (see gotcha).
- **launchd service note:** local server now runs as `~/Library/LaunchAgents/com.rehearsal-planner.dev.plist` (not committed; machine-specific). See Local Development.

### Session: Song Library & Set List Toolbars (2026-05-31)

Removed the duplicate panel title on mobile and added filter/grouping toolbars to both panels. **All in `templates/index.html`.** Branch: `library-setlist-toolbars`, PR #2: https://github.com/jmsyng/rehearsal-planner/pull/2.

**What was built:**

- **Mobile title deduplication.** On mobile (≤800px) the panel name was shown twice — once in the `#mobile-tab-bar` tab and again in the `.panel-header h2`. Fixed with a single CSS rule: `@media (max-width: 800px) { .panel-header h2 { display: none; } }`. Desktop keeps both.

- **Song Library toolbar** (`.panel-tools` inside `.panel-header`):
  - `#lib-filter-tuning` — filter by tuning (dropdown). Options are derived from live `libraryData` on each render call (`populateLibraryFilters()`), so only actually-present tunings appear.
  - `#lib-filter-status` — filter by status (dropdown). Uses `effectiveStatus(song)` helper: Archived → 'Archived', else `proposalStatus` (pending→Proposed, approved→Learning, rejected→Resting), else `songStatus || Status`.
  - `#lib-show-archived` — show/hide archived songs toggle (button with `aria-pressed`). Default off (archived hidden, matching prior behavior).
  - Count chip reads "N of M" whenever any filter is active (`isNarrowed` boolean).

- **Set List toolbar:**
  - `#set-group-by` — "Group by tuning" one-shot action. Stable-groups `setListIds` by `OurTuning || Tuning` (preserving first-appearance order for both groups and members), mutates `setListIds`, and calls `saveSetlistDebounced()` → persists the new order to the DB. Select resets to `''` after firing so it reads as an action, not a persistent mode.

- **Toolbar styling.** New `.panel-tools`, `.toolbar-select`, `.toolbar-toggle` classes. Compact dropdowns styled like the existing `.hdr-search` bar (rounded, 1px border, `focus-within` accent). Per-panel themed overrides scoped under `#library-panel` (cyan) and `#setlist-panel` (amber). `.panel-header` changed from `justify-content: space-between` to `gap: 8px; flex-wrap: wrap` so controls wrap gracefully on narrow screens.

**New JS globals/helpers added near `~2039`:** `libraryTuningFilter`, `libraryStatusFilter`, `libraryShowArchived`, `STATUS_ORDER`, `effectiveStatus()`.

**New JS functions after `renderLibrary()`:** `populateLibraryFilters()` (rebuilds tuning + status selects from current library), `rebuildSelect(sel, values, current, allLabel)` (generic select rebuilder, toggles `.active` class), `groupSetListByTuning()` (one-shot reorder + save).

**Also in this session:** Added a session-start guardrail to the top of `CLAUDE.md` (after the `# Rehearsal Planner` title) to prevent future sessions from assuming a React/TypeScript stack. Prompted by two consecutive sessions that fired a "tampering alarm" after flooding `Read` calls for nonexistent `src/App.tsx` etc., then misread the "file not found" flood as adversarial injection.

**PR state at session end:** Two commits on `library-setlist-toolbars` — `441023f` (toolbar feature) and `03d332c` (CLAUDE.md guardrail). Not yet merged. Preview testing requires adding the branch's Vercel preview origin to Neon Trusted Origins first.

**Uncommitted:** `app.py` has a local configurable-port change (`python3 app.py [port]` or `$PORT`) that wasn't part of this session and wasn't put in the PR.

### Session: Multi-band/multi-setlist schema migration + code alignment (2026-05-31)

Completed the code changes to align with the new database schema (designed and migrated in the prior summarized session). Committed and deployed.

**What landed in this session:**

- **`app.py` — three fixes:**
  - `api_add_song`: `db.create_proposal(band_id, song["id"], ...)` → `saved["id"]`; matching band-song lookup loop likewise. The proposal must reference the DB UUID, not the client string the browser sent.
  - `get_initial_songs`: was discarding the return value of `db.upsert_song()`, so the browser got back `{"id": "song-0", ...}` placeholder strings. Now captures `saved` and returns it — browser sees real UUIDs from the first load.
  - Validation relaxed: only `name` is required to add a song; `id` is optional. Custom songs have no external identifier, so requiring it was wrong.

- **`templates/index.html` — three fixes:**
  - `addFromSpotify`: `id: \`song-spotify-${Date.now()}\`` → `id: track.id` (real Spotify track ID). Stored as `external_id`; enables duplicate guard on re-add.
  - `addSpotifySong` (Add Song modal): same ID fix; also added missing `spotifyUrl: track.spotify_url` to `extra` (was silently absent from the modal path).
  - `addCustomSong`: removed `id` field entirely — no `id` sent → `external_id = NULL` in DB. Intentional: custom songs are allowed to be duplicated by name.

**The rule going forward — song IDs:**
- Song `id` is **always server-assigned (a UUID)**. The frontend must use the `id` in the API response — never generate its own.
- Spotify songs: send `id: track.id` in the add payload. Server stores it as `external_id`; returns a UUID as `id`.
- Custom songs: omit `id`. Server assigns a UUID; `external_id` stays NULL.

**Migration tooling (added in prior session, deployed here):**
- `migrate.py` — apply any unapplied `migrations/*.sql` files; tracks state in `schema_migrations`; all-or-nothing transactions; safe to run any time.
- `init_db.py` — **fresh databases only**. Running it on an existing DB will silently skip tables that already exist but won't apply incremental changes — use `migrate.py` for that. After applying `schema.sql`, it **pre-stamps `schema_migrations`** with every current `migrations/*.sql` filename (`ON CONFLICT DO NOTHING`), since `schema.sql` already reflects the post-migration target state. So a fresh DB is born "up to date": a subsequent `python3 migrate.py` is correctly a no-op and won't try to replay migration 001 (which references long-gone legacy tables `set_lists`/`set_list_songs`/`band_set_list_songs`) against the post-001 schema and crash.
- For new schema changes: write `migrations/NNN_description.sql`, run `migrate.py` locally to test, then deploy. Update `schema.sql` to match so it stays the canonical description.

**`db.py` (full rewrite completed in prior session, deployed here):**
- All public function names/signatures preserved — `app.py` callers were minimally disrupted.
- `upsert_song`: `song['id']` is a UUID → UPDATE existing row; otherwise → CREATE with incoming as `external_id` (ON CONFLICT → graceful upsert).
- `_default_setlist_id()`: hides multi-setlist schema behind single-setlist behaviour for backward compat. Schema supports multiple named setlists per band/user; UI still uses one — multi-setlist UI is future work.
- `plays` is pulled from `setlist_songs` via a correlated subquery (`_PLAYS_SUBQUERY`), not from `songs` (column was removed in migration 001).

**Deployment:**
- Committed everything on `library-setlist-toolbars`, merged to `main` (fast-forward), pushed → Vercel auto-deployed.
- Migration 001 was already applied to the Neon DB before this deploy (done in prior session). **Never deploy schema-breaking code before running the migration.**

**Stale documentation updated in this session:**
- Database Schema section — now reflects `setlists`/`setlist_songs`, removed mentions of `set_lists`/`set_list_songs`/`band_set_list_songs`
- File Layout — added `migrate.py` and `migrations/`
- Common Tasks — corrected "Apply schema changes" and "Wipe all app data" rows

### Session: Vote modal fixes — submit bug, queue nav, mobile buttons, scoring (2026-06-04)

Fixed the "Vote on Songs" modal and reworked the 5-point scoring to be band-size-driven.

**The "Failed to submit vote" bug (`db.py`) — the important one.** Casting a vote that
**crossed the approval threshold** (e.g. voting "Love it" to push a song over) 500'd, so the
frontend showed "Failed to submit vote." Root cause: `cast_vote` runs on a `RealDictCursor`,
so when its approval path called `_auto_add_to_setlist` → `_default_setlist_id`, those helpers
did positional `row[0]` / `cur.fetchone()[0]` and raised `KeyError: 0` (dict rows have no key
`0`). Votes that *didn't* cross the threshold never hit the auto-add path, so it looked
intermittent. Fix: both helpers now read the column by name when the row is a dict, falling
back to index for tuple cursors (`row["id"] if isinstance(row, dict) else row[0]`, and an
aliased `next_pos` column in `_auto_add_to_setlist`). **Lesson: any helper that may be called
with either a tuple cursor or a `RealDictCursor` must not index rows positionally.** Verified
end-to-end against the live DB (River Below proposal → approved, score 10, song→Learning,
auto-added to setlist) then reverted.

**Scoring rework (decisions from the user):**
- **Default approval factor lowered 3.5 → 3.0** (~60%, simple positive-leaning majority on the
  5-point scale). `schema.sql` default changed; `migrations/002_approval_factor_default_3.sql`
  sets the column default to 3.0 and migrates bands still on the old default (`= 3.5`) to 3.0.
  Applied locally via `migrate.py` (the one existing band is now 3.0). **Run `migrate.py`
  against prod before/with deploy.**
- **4-member cap removed entirely** (`db.py join_band` — deleted the `count >= 4` /
  "Band is full" check). Scoring already scales off the live `band_members` count, so any size
  works. UI "Members (n/4)" → "Members (n)".
- **Frontend was hardcoding `3.5`** in the score lines (inline card + voting modal) and had a
  `|| 4` band-size fallback. Both now read `_bandData.approval_factor` (fallback 3.0) and use
  the real `members.length` (fallback 1). The Band Settings slider help text already used the
  live factor.

**Voting modal UX (`templates/index.html`, `renderVotingCard`):**
- **Queue navigation added.** The modal only ever showed `_votingProposals[0]` and spliced on
  vote — no way to browse the others. Added "‹ Previous" / "Skip ›" buttons (shown when >1
  proposal; ends disabled appropriately) that move `_votingIndex` and re-render. Progress line
  now reads "Song X of N waiting for your vote". Added an index clamp at the top of
  `renderVotingCard` so a splice past the end can't leave `_votingIndex` out of range.
- **Mobile vote buttons** were "❤️ Love it" … ×5 in one row — far too tight. Now each button
  shows the emoji + its **point value** (5/4/3/2/1) stacked, with the descriptive label moved
  to `title`/`aria-label`, plus a "1 · Hard no … 5 · Love it" legend under the row. (The inline
  song-card vote buttons were already emoji-only.)

**Verification:** vote bug verified against live DB (above). Modal verified in Claude Preview
(localhost:5051) at 375px with injected mock `_bandData`/`_votingProposals` (no real band on the
test account): point-value buttons + legend, Prev/Skip nav across 3 songs with correct
disabled-end states, score "5/10 · need 6 to approve" (2 members × 3.0), and a 5-member sim
showing "5/25 · need 15" — confirming size-independence. No console errors.

**State at session end:** All changes local only (not committed, not deployed). Migration 002
applied to the local/connected Neon DB only.

### Session: Set List Sort By Feature (2026-06-04)

Added a "Sort By" control to the set list toolbar. **All in `templates/index.html`.** Branch: `claude/set-list-sort-feature-fQ2Fz`, PR #12 — merged and deployed.

**What was built:**

- **`<select id="set-sort-by">` toolbar control** — sits next to "Group by tuning" in the set list panel header. Options: Manual order (default) / Artist A–Z / Title A–Z / Duration ↑. Styled via existing `.toolbar-select`; highlights amber (`.active`) when a non-default sort is active.

- **Ephemeral sort lens** — `setListSortBy` is a view-only state variable. `setListIds` (the DB-backed order) is **never modified** by the sort. `applySongSort(arr)` returns a sorted copy of the songs array; `renderSetList()` calls it after the search filter, before the flat/grouped render branch — so one insertion covers both paths.

- **Drag-to-bake** — when the user drags a card while a sort is active, the `onUpdate` handler reads the full sorted DOM order into `setListIds`, clears `setListSortBy`, and saves. Dragging is treated as "commit this arrangement." Same bake-and-clear added to `syncSetListFromDom()` (the grouped-mode drag handler).

- **Compound sort with grouping** — when both "Group by tuning" and a sort are active, `applySongSort` runs on the `songs` array before it's passed to `renderSetListGrouped`, so songs within each tuning group are ordered by the chosen criterion.

**New globals/helpers:** `let setListSortBy = ''` (state), `applySongSort(arr)` (pure helper before `groupSetListByTuning`).

**Adjacent features considered but deferred:** descending/reverse toggle, sort-by-plays, filter-by-tuning on the set list side, `localStorage` sort persistence, position-number opacity dimming while sorted.

**No DB migration needed** — pure frontend change.

### Session: Vote Submit Fix, Threshold Rework & Modal Polish (2026-06-04)

**Bugs fixed:**

- **"Failed to submit vote" (500 crash).** `cast_vote` triggered auto-add-to-setlist when a proposal crossed the approval threshold. Two helpers (`_default_setlist_id`, `_auto_add_to_setlist`) read results as `row[0]` (positional), but `cast_vote` opens a `RealDictCursor` — `KeyError: 0`. Fixed to read by column name. This was the root cause of the notification-bell vote failure.

- **4-member band cap removed from `db.join_band`; sanity ceiling of 24 added.** Previous code hard-capped bands at 4 members. Now joins fail only if the band already has ≥ 24 members.

**Scoring model updated (`db.py`, `schema.sql`, `templates/index.html`):**

- `approval_factor` default raised from **3.0 → 3.25**. At 3.25 a 4-member band needs **13/20** to approve — meaning 4 Mehs (12) and 2 Love+2 Hard-no (12) both fall below the bar. Scales to other sizes: 2→7/10, 3→10/15, 5→17/25, 6→20/30.
- Migration **003** (`migrations/003_approval_factor_3_25.sql`) updates existing bands still on 3.0 to 3.25. Bands with a custom value are left alone.
- All JS fallback references updated from `?? 3.0` → `?? 3.25`. Band Settings slider default display updated to match.

**Voting modal polish (`renderVotingCard`):**

- **Score line** simplified to `Score: N (X pts to approve)` — no more fractions or "avg per member" phrasing.
- **Legend** corrected: was `1 · Hard no … 5 · Love it` (reversed vs the buttons); now `5 · Love it … 1 · Hard no`.
- **"Skip ›" → "Next ›"** on the queue navigation button.
- **Members count** in Band Settings header now shows `Members (N/24)`.

**Deployment:** committed and pushed to `main` in one commit (`7e1155a`). Migrations were already applied to Neon DB before push.

### Session: Multi-Setlist / Timing Overhaul + Plays Isolation (2026-06-04)

Resolved merge conflicts on the Phase B branch, fixed a critical band-setup regression, shipped the timing/multi-setlist overhaul (PR #15), and isolated play counts per setlist (PR #17). PR #16 (read-only share links, separate session) also merged.

**Merge conflict resolution (`claude/planning-mode-6FLPk`):**

Main had moved 14 commits ahead. Two conflicts in `templates/index.html`:
- Set List toolbar: kept both `#set-sort-by` select (from main) and `⏱` timing button (from branch).
- `updateTimeBar` call site: used no-args form `updateTimeBar()` + `requestAnimationFrame(applyNameMarquee)`.

**Critical regression fix — band-setup overlay on reload (`db.py`):**

Phase B's `get_user_band` queried `default_*` timing columns added by migration 004. If migration hadn't been applied to the production DB, psycopg2 raised `UndefinedColumn` → 500 → `_bandData` null → band-setup overlay shown to users already in a band. Three-layer defensive fix:
1. `get_user_band`: `try/except psycopg2.errors.UndefinedColumn` retries with a simpler query omitting the new columns.
2. `_settings_from_row`: uses `.get()` with `SOLO_DEFAULT_SETTINGS` fallback instead of direct dict indexing.
3. `_default_setlist_id`: savepoint pattern so the timing-column `INSERT` can fall back to a plain `INSERT` if migration absent.
**Lesson:** always add `try/except UndefinedColumn` defensive fallbacks in any `db.py` function that reads newly-added columns, so code can deploy before the migration runs.

**Plays per setlist isolation (PR #17, `db.py` + `templates/index.html`):**

The DB schema (`setlist_songs.plays` keyed by `(setlist_id, song_id)`) was already correct. The bug was on the read side: `_PLAYS_SUBQUERY` fetched plays from the earliest-created setlist regardless of which setlist was active, so plays bled across setlists when switching.

- **`db.py`**: removed `_PLAYS_SUBQUERY` entirely. `get_songs` and `get_band_songs` now return `1 AS plays`. Comment added explaining that the correct per-setlist value is overlaid by the frontend after `GET /api/setlist`.
- **`templates/index.html`**: `applySetlistResponse` resets any song not in the incoming setlist to `plays = 1` before overlaying the new setlist's values. Songs entering a setlist for the first time always start at 1 play.

**PRs merged:** #15 (multi-setlist + timing overhaul), #17 (plays isolation), #16 (read-only share links).

**`_PLAYS_SUBQUERY` is gone — do not re-add it.** Songs always load with `plays: 1`; `applySetlistResponse` is the sole source of truth for plays in the UI.

### Session: Read-only Shareable Set List View (2026-06-10)

Added a public, no-auth shareable link for each setlist. Branch: `claude/readonly-shareable-view-oBREc`, PR #16 — merged and deployed. Follow-up fixes in PR #18.

**What was built (PR #16):**

- **`migrations/005_setlist_share_token.sql`** — `ALTER TABLE setlists ADD COLUMN IF NOT EXISTS share_token TEXT UNIQUE DEFAULT gen_random_uuid()::text`. Postgres evaluates the volatile default per existing row, so every current setlist got a distinct token. Also updated `schema.sql` to include the column.

- **`db.py` — two new public functions:**
  - `get_setlist_share_token(setlist_id)` — returns the `share_token` for a specific setlist by ID.
  - `get_shared_setlist(token)` — public lookup with no auth. Resolves owner label (band name or `COALESCE(display_name, name, email)`), joins `setlist_songs → songs` for display-only fields, includes `settings` dict (target/warn/buffer/breaks from the setlist row), returns `{name, ownerName, settings, songs}`. Deliberately omits proposals, votes, emails — nothing private leaks.

- **`app.py` — three new routes:**
  - `GET /share/<token>` — public page, renders `index.html` with `share_token=token`.
  - `GET /api/shared/<token>` — public data endpoint, returns `get_shared_setlist` result or 404.
  - `GET /api/setlist/share` (`@require_auth`) — resolves the caller's active setlist via `_resolve_setlist`, returns `{"token": ...}`. Frontend builds the full URL from `location.origin`.
  - `index()` also now passes `share_token=""` so the normal page renders with an empty token.

- **`templates/index.html` — `IS_SHARED_VIEW` read-only mode:**
  - `SHARE_TOKEN` read from a `<meta name="share-token">` tag (server-injected); `IS_SHARED_VIEW = !!SHARE_TOKEN`.
  - Bootstrap IIFE: `if (IS_SHARED_VIEW) { loadSharedView(); return; }` — bypasses all auth/JWT logic entirely.
  - `loadSharedView()`: hides auth overlays + all owner chrome (add button, bell, band/user menus, library panel, toolbar), sets `_setlistSettings = data.settings` so `updateTimeBar()` uses the correct thresholds, populates `libraryData`/`setListIds`, calls `renderSetList()`.
  - `showShareError()`: friendly "This link isn't valid" message for bad/expired tokens.
  - `makeSongCard()`: `IS_SHARED_VIEW` guards suppress drag handle, expand button, add/remove buttons, plays +/− stepper (replaced with static count), status chip (span not button), and the entire expanded block.
  - `renderSetList()`: `Sortable.create` and `contextmenu` handler skipped when `IS_SHARED_VIEW`.

**Follow-up fixes (PR #18):**

- **Share link moved to ☰ band menu** — toolbar button was wrapping off-screen on smaller panels. Now at ☰ → "🔗 Copy set list link".
- **`migrations/006_deduplicate_setlists.sql`** — removes empty duplicate setlists created during the window when migration 004 was not yet applied to prod. The `_default_setlist_id` savepoint fallback created a new empty row on each app load that tried the timing-column INSERT and failed; 006 keeps the setlist with the most songs per owner (oldest if tied) and deletes the rest, but only the empty ones (never deletes a setlist with songs). Applied to prod via `migrate.py`.
- **Diagnosis tip:** the symptoms (ten "Main Set" entries in the switcher, create-setlist failing) were root-caused via **Vercel runtime logs** (`get_runtime_logs` MCP, project `prj_9GclRIPlSzABOMogn4Y0reP0E60t`, team `team_50uzQ1PIUjJUImH5JjwGj9Np`) — a `POST /api/setlists 500` before migration 004 landed, then `201`s after. When a prod UI bug is reported, check those logs first.
- **Migration ordering gotcha:** `migrate.py` reads the migration files **from the checked-out branch**, not from the DB. A stale `git checkout` (branch ref pointed at an old commit) silently skipped 006 — `git pull origin <branch>` first, then re-run.

### Session: Setlist Dedup, Active-Set Persistence & Shows (2026-06-10)

Fixed the two reported setlist bugs and added a Set List Management interface (shows that group multiple sets in one night). Plan file: `~/.claude/plans/new-features-aren-t-working-rustling-alpaca.md`.

**Bug A — duplicate "Main Set" (data + determinism):**
- Root cause: `migrations/001` line ~168 did `SELECT DISTINCT gen_random_uuid(), 'Main Set', bsls.band_id FROM band_set_list_songs` — the per-row `gen_random_uuid()` defeats `DISTINCT`, minting one "Main Set" per *song row*. Band 14a19412 had 11 of them, all sharing one `created_at`. `_default_setlist_id`'s `ORDER BY created_at LIMIT 1` then picked non-deterministically on the tie, scattering saves.
- `migrations/007_deduplicate_setlists_with_songs.sql` — among setlists sharing `(owner, name, created_at)`, keeps the most-songs row (lowest id on ties), backs the rest up into `setlists_dedup_backup` / `setlist_songs_dedup_backup`, then deletes. Applied to prod via `migrate.py` (deleted 9 rows, kept the 24-song keeper; backups hold 9 setlists / 61 songs).
- Fixed `001` in place (DISTINCT the band_ids first — inert on prod, protects fresh replays) and added `, id` tie-breakers to every setlist `ORDER BY created_at` in `db.py`.

**Bug B — active set "doesn't persist":** two compounding causes. (1) `GET /api/setlist` returned the id under key `id`, but `applySetlistResponse` reads `data.setlist_id` → `_currentSetlistId` never set → switcher couldn't highlight and saves hit the default. `get_setlist_full` now also returns `setlist_id`. (2) `loadAppData` wrote the `rp_setlist_<owner>` localStorage key (via `applySetlistResponse` on the default set) *before* `loadActiveSetlist` read it, clobbering the choice every load. Now reads the stored id up front, fetches it directly in the parallel batch, and falls back to default (clearing the key) on a stale 404/400. `loadActiveSetlist` removed.

**Feature — Shows:** `migrations/008_shows.sql` + `schema.sql`: new `shows` table (band_id XOR user_id, name, show_date, venue, notes) and nullable `setlists.show_id` + `position` (order within show; `ON DELETE SET NULL` so deleting a show demotes its sets to standalone). `db.py`: `list_setlists` now LEFT JOINs a song aggregate (`song_count`, `total_seconds`) and returns `show_id`/`position`; new `list_shows`/`create_show`/`update_show`/`delete_show`/`set_show_set_order`/`assign_setlist_to_show`/`duplicate_setlist` mirror the `rename_setlist` ownership/commit pattern. `app.py`: `GET/POST /api/shows`, `PATCH/DELETE /api/shows/<id>` (PATCH also takes `set_order` for drag-reorder), `POST /api/setlists/<id>/duplicate`; `api_create_setlist`/`api_update_setlist` extended for `show_id` (null = standalone). Frontend: `_shows` global, 5th `/api/shows` fetch in `loadAppData`, optgrouped `renderSetlistSwitcher`, new `🗂` toolbar button + `#setlist-manager-modal` (copies the Band-Settings modal pattern) with `renderSetlistManager`/`initManagerSortables` (per-show SortableJS) and `manager*` CRUD handlers.

**Verified:** migration dry-run + read-only SQL (0 dup groups, keeper 24 songs, backups populated, shows schema present); full API smoke on :5050 (`devtest_signin@example.com`/`DevTest!2345`) — determinism, show CRUD, duplicate, reorder, move, cascade-to-standalone, bad-date 400; UI on preview :5051 — optgroups, manager modal, Bug-B persistence across a simulated reload, stale-id fallback, mobile layout. **Note:** the dev test account is solo (no band) so the app routes it to band-setup; verification drove the solo path directly via `preview_eval` (set `_currentUserId`, call `loadAppData()`). `.claude/launch.json` corrected to bind **5051** (was 5050, which collides with the launchd server).

### Session: Deployment verification & tooling (2026-06-12)

Continuation of the 2026-06-10 session — verified and shipped everything.

**`gh` installed:** `brew install gh` (Homebrew's git editor opened mid-install; `:wq` to accept the merge commit). Already authenticated as `jmsyng` via keyring. Use `gh auth status` to confirm in future sessions.

**Vercel deploy confirmed:** PR #19 (`claude/setlist-shows-management`) was merged to main via the GitHub web UI. Production deployment `dpl_FT57qkEJqY7E7qNaFjAbovPdbHfi` is `READY` (target: production). Vercel project id `prj_9GclRIPlSzABOMogn4Y0reP0E60t`, team `team_50uzQ1PIUjJUImH5JjwGj9Np`.

**Git cleanup needed after this session:** local main had an aborted merge commit (Homebrew's git editor exited with empty message → "aborted"). Resolved by running `git reset --hard origin/main` to sync local main with the already-merged remote.

**UI verified on :5051 (preview MCP):** manager modal opens, show creation works, sets move into shows and appear in optgroups in the switcher, Bug B (reload persistence) confirmed, no console errors. Test data (one show, one duplicate set) cleaned up via the API after verification.

## Maintenance Pattern for This File

When future sessions do meaningful work in this project, ask Claude:

> "Append a section to CLAUDE.md describing what we did in this session. Use existing sections as a style reference. Don't bloat — only include things future sessions will need to know."

That way this file grows incrementally without you having to write it.
