import json
import math
import os
import uuid as _uuidlib
import psycopg2
from psycopg2.extras import RealDictCursor

DEFAULT_TUNINGS = ['E standard', 'Eb', 'Drop D', 'Drop C#']

# Default timing settings (matches migration 004 / schema.sql defaults).
# Used to seed solo-user setlists and as the fallback in get_setlist_full.
SOLO_DEFAULT_SETTINGS = {
    "target_seconds": 9000,
    "warn_seconds": 7200,
    "song_buffer_seconds": 45,
    "tuning_change_seconds": 60,
    "break_count": 0,
    "break_seconds": 0,
}
_SETTINGS_COLS = list(SOLO_DEFAULT_SETTINGS.keys())
_BAND_DEFAULT_COLS = [f"default_{c}" for c in _SETTINGS_COLS]

# 5-point Likert scale — stored as strings "1"–"5"
VOTE_POINTS = {"5": 5, "4": 4, "3": 3, "2": 2, "1": 1}
# Approval threshold = math.ceil(band_size * approval_factor), computed in cast_vote.
# approval_factor is per-band (bands.approval_factor, default 3.0/5) — never assume
# a fixed band size; everything scales off the live band_members count.


def get_conn():
    """Open a fresh Postgres connection. Recommended for Neon (serverless)."""
    return psycopg2.connect(os.environ["DATABASE_URL"])


def put_conn(conn):
    """Close the connection (no pooling). Safe to call even if already closed."""
    try:
        conn.close()
    except Exception:
        pass


def _is_uuid(value) -> bool:
    """True if `value` is a syntactically valid UUID (i.e. a server-assigned song id)."""
    try:
        _uuidlib.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


# ── Songs ──────────────────────────────────────────────────────────────────────
#
# NEW MODEL (post multi-band/multi-setlist migration):
#   • songs.id is a server-assigned UUID (was a client string).
#   • the old client string lives on as songs.external_id (Spotify ID or custom).
#   • a song belongs to exactly ONE owner: band_id XOR user_id (DB CHECK enforces it).
#   • `plays` no longer lives on the song — it's per-setlist (setlist_songs.plays).
#     We surface a song's play count from its owner's default setlist so the current
#     UI keeps showing the right number.

def _song_fields(song: dict) -> dict:
    """Map the JS song object's editable fields to DB columns."""
    extra = song.get("extra") or {}
    return {
        "name":            song["name"],
        "duration_raw":    song.get("duration_raw", ""),
        "duration_sec":    song.get("duration_seconds", 0),
        "artist":          extra.get("Artist", ""),
        "status":          extra.get("Status", "For Consideration"),
        "tuning":          extra.get("Tuning"),
        "recorded_tuning": extra.get("RecordedTuning"),
        "our_tuning":      extra.get("OurTuning"),
        "album_art":       extra.get("albumArt"),
        "spotify_url":     extra.get("spotifyUrl"),
        "youtube_link":    extra.get("YouTubeLink"),
    }


def _row_to_song(row) -> dict:
    """Convert a DB row to the JS-compatible song object shape."""
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "duration_raw": row["duration_raw"],
        "duration_seconds": row["duration_sec"],
        "plays": row.get("plays") or 1,
        "extra": {
            "Artist": row["artist"],
            "Status": row["status"],
            "Tuning": row["tuning"],
            "RecordedTuning": row["recorded_tuning"],
            "OurTuning": row["our_tuning"],
            "albumArt": row["album_art"],
            "spotifyUrl": row["spotify_url"],
            "YouTubeLink": row["youtube_link"],
            "externalId": row.get("external_id"),
        }
    }


def _row_to_band_song(row) -> dict:
    """Like _row_to_song but includes band proposal metadata."""
    song = _row_to_song(row)
    song["extra"]["proposerName"]    = row.get("proposer_name")
    song["extra"]["proposedBy"]      = row.get("proposer_id")
    song["extra"]["proposalId"]      = str(row["proposal_id"]) if row.get("proposal_id") else None
    song["extra"]["proposalStatus"]  = row.get("proposal_status")
    song["extra"]["proposalScore"]   = row.get("proposal_score")
    song["extra"]["userVote"]        = row.get("user_vote")
    song["extra"]["userVoteReason"]  = row.get("user_vote_reason")
    raw_votes = row.get("proposal_votes")
    if isinstance(raw_votes, str):
        raw_votes = json.loads(raw_votes)
    song["extra"]["proposalVotes"]   = raw_votes or []
    return song


# Correlated subquery that pulls a song's play count from its owner's default
# (earliest-created) setlist, so _row_to_song's `plays` matches the old behaviour.
_PLAYS_SUBQUERY = """
    (SELECT ss.plays
       FROM setlist_songs ss
       JOIN setlists sl ON sl.id = ss.setlist_id
      WHERE ss.song_id = s.id AND sl.{owner_col} = %(owner)s
      ORDER BY sl.created_at
      LIMIT 1) AS plays
"""


def get_songs(user_id: str) -> list:
    """A user's personal library (songs they own directly, not via a band)."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT s.*, " + _PLAYS_SUBQUERY.format(owner_col="user_id") +
                " FROM songs s WHERE s.user_id = %(owner)s ORDER BY s.created_at",
                {"owner": user_id}
            )
            rows = cur.fetchall()
        return [_row_to_song(r) for r in rows]
    finally:
        put_conn(conn)


def upsert_song(user_id: str, song: dict, band_id: str = None) -> dict:
    """
    Create or update a song.
      • If song['id'] is an existing song UUID owned by this owner -> UPDATE it.
      • Otherwise CREATE a new song; song['id'] becomes external_id, and a fresh
        UUID is assigned. If that external_id already exists in this library, the
        existing row is updated (duplicate guard -> graceful upsert).
    Owner is the band (if band_id given) else the user. added_by is always the actor.
    Returns the saved song (with its real UUID id).
    """
    f = _song_fields(song)
    incoming = str(song.get("id") or "")
    ins_band_id = band_id
    ins_user_id = None if band_id else user_id
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # ── UPDATE path: incoming id is an existing UUID for this owner ──
            if _is_uuid(incoming):
                if band_id:
                    # Band members may update band-owned songs or their own personal songs
                    update_pred = "band_id = %(band_id)s OR user_id = %(user_id)s"
                    update_params = {**f, "id": incoming, "band_id": band_id, "user_id": user_id}
                else:
                    update_pred = "user_id = %(user_id)s"
                    update_params = {**f, "id": incoming, "user_id": user_id}
                cur.execute(f"""
                    UPDATE songs SET
                      name=%(name)s, duration_raw=%(duration_raw)s, duration_sec=%(duration_sec)s,
                      artist=%(artist)s, status=%(status)s, tuning=%(tuning)s,
                      recorded_tuning=%(recorded_tuning)s, our_tuning=%(our_tuning)s,
                      album_art=%(album_art)s, spotify_url=%(spotify_url)s, youtube_link=%(youtube_link)s
                    WHERE id=%(id)s AND ({update_pred})
                    RETURNING *
                """, update_params)
                row = cur.fetchone()
                if row:
                    conn.commit()
                    return _row_to_song(row)
                # fall through to CREATE if the UUID didn't match a row for this owner

            # ── CREATE path ──
            external_id = None if _is_uuid(incoming) else (incoming or None)
            params = {**f, "external_id": external_id, "band_id": ins_band_id,
                      "user_id": ins_user_id, "added_by": user_id}

            if external_id is None:
                # No external id -> no duplicate guard applies -> plain insert.
                cur.execute("""
                    INSERT INTO songs
                      (external_id, band_id, user_id, added_by, name, duration_raw,
                       duration_sec, artist, status, tuning, recorded_tuning, our_tuning,
                       album_art, spotify_url, youtube_link)
                    VALUES
                      (%(external_id)s, %(band_id)s, %(user_id)s, %(added_by)s, %(name)s,
                       %(duration_raw)s, %(duration_sec)s, %(artist)s, %(status)s, %(tuning)s,
                       %(recorded_tuning)s, %(our_tuning)s, %(album_art)s, %(spotify_url)s,
                       %(youtube_link)s)
                    RETURNING *
                """, params)
            else:
                conflict_col = "band_id" if band_id else "user_id"
                cur.execute(f"""
                    INSERT INTO songs
                      (external_id, band_id, user_id, added_by, name, duration_raw,
                       duration_sec, artist, status, tuning, recorded_tuning, our_tuning,
                       album_art, spotify_url, youtube_link)
                    VALUES
                      (%(external_id)s, %(band_id)s, %(user_id)s, %(added_by)s, %(name)s,
                       %(duration_raw)s, %(duration_sec)s, %(artist)s, %(status)s, %(tuning)s,
                       %(recorded_tuning)s, %(our_tuning)s, %(album_art)s, %(spotify_url)s,
                       %(youtube_link)s)
                    ON CONFLICT ({conflict_col}, external_id)
                      WHERE {conflict_col} IS NOT NULL AND external_id IS NOT NULL
                    DO UPDATE SET
                      name=EXCLUDED.name, duration_raw=EXCLUDED.duration_raw,
                      duration_sec=EXCLUDED.duration_sec, artist=EXCLUDED.artist,
                      status=EXCLUDED.status, tuning=EXCLUDED.tuning,
                      recorded_tuning=EXCLUDED.recorded_tuning, our_tuning=EXCLUDED.our_tuning,
                      album_art=EXCLUDED.album_art, spotify_url=EXCLUDED.spotify_url,
                      youtube_link=EXCLUDED.youtube_link
                    RETURNING *
                """, params)
            row = cur.fetchone()
        conn.commit()
        return _row_to_song(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def delete_song(user_id: str, song_id: str) -> bool:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM songs WHERE id = %s AND user_id = %s",
                (song_id, user_id)
            )
            deleted = cur.rowcount > 0
        conn.commit()
        return deleted
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def delete_band_song(band_id: str, song_id: str) -> bool:
    """Delete a song from the band library (any member can delete)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM songs WHERE id = %s AND band_id = %s",
                (song_id, band_id)
            )
            deleted = cur.rowcount > 0
        conn.commit()
        return deleted
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def get_band_songs(band_id: str, user_id: str) -> list:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT s.*,
                       """ + _PLAYS_SUBQUERY.format(owner_col="band_id") + """,
                       COALESCE(up.display_name, nu.name, nu.email) AS proposer_name,
                       sp.id              AS proposal_id,
                       sp.proposed_by::text AS proposer_id,
                       sp.status          AS proposal_status,
                       sp.score           AS proposal_score,
                       sv_me.vote         AS user_vote,
                       sv_me.reason       AS user_vote_reason,
                       (SELECT json_agg(json_build_object(
                                  'vote', sv2.vote,
                                  'name', COALESCE(up2.display_name, nu2.name, nu2.email),
                                  'reason', sv2.reason))
                          FROM song_votes sv2
                          JOIN neon_auth."user" nu2 ON nu2.id = sv2.user_id
                          LEFT JOIN user_profiles up2 ON up2.user_id = sv2.user_id
                         WHERE sv2.proposal_id = sp.id
                       ) AS proposal_votes
                FROM songs s
                LEFT JOIN neon_auth."user" nu ON nu.id = s.added_by
                LEFT JOIN user_profiles up ON up.user_id = s.added_by
                LEFT JOIN song_proposals sp
                       ON sp.song_id = s.id
                      AND sp.band_id = %(owner)s
                      AND sp.status  IN ('pending', 'approved')
                LEFT JOIN song_votes sv_me
                       ON sv_me.proposal_id = sp.id
                      AND sv_me.user_id = %(user_id)s
                WHERE s.band_id = %(owner)s
                ORDER BY s.created_at
            """, {"owner": band_id, "user_id": user_id})
            rows = cur.fetchall()
        return [_row_to_band_song(r) for r in rows]
    finally:
        put_conn(conn)


# ── Set Lists ────────────────────────────────────────────────────────────────────
#
# The schema supports many named setlists per owner. Until the multi-setlist UI
# exists, the app uses ONE default setlist per owner: the earliest-created one,
# created on demand. These helpers hide that behind the old single-setlist API.

def _default_setlist_id(cur, *, user_id=None, band_id=None, create=False):
    """Return the owner's default (earliest) setlist id, optionally creating one."""
    if band_id:
        cur.execute(
            "SELECT id FROM setlists WHERE band_id = %s ORDER BY created_at LIMIT 1",
            (band_id,)
        )
    else:
        cur.execute(
            "SELECT id FROM setlists WHERE user_id = %s ORDER BY created_at LIMIT 1",
            (user_id,)
        )
    row = cur.fetchone()
    if row:
        # Works whether `cur` is a tuple cursor or a RealDictCursor (e.g. when
        # called from cast_vote → _auto_add_to_setlist).
        return row["id"] if isinstance(row, dict) else row[0]
    if not create:
        return None
    # Seed new setlist with timing settings from band defaults (if band) or
    # SOLO_DEFAULT_SETTINGS (if solo / band columns not available).
    if band_id:
        cur.execute("""
            INSERT INTO setlists (name, band_id,
                target_seconds, warn_seconds, song_buffer_seconds,
                tuning_change_seconds, break_count, break_seconds)
            SELECT 'Main Set', %s,
                b.default_target_seconds, b.default_warn_seconds, b.default_song_buffer_seconds,
                b.default_tuning_change_seconds, b.default_break_count, b.default_break_seconds
            FROM bands b WHERE b.id = %s
            RETURNING id
        """, (band_id, band_id))
    else:
        d = SOLO_DEFAULT_SETTINGS
        cur.execute("""
            INSERT INTO setlists (name, user_id,
                target_seconds, warn_seconds, song_buffer_seconds,
                tuning_change_seconds, break_count, break_seconds)
            VALUES ('Main Set', %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (user_id, d["target_seconds"], d["warn_seconds"], d["song_buffer_seconds"],
              d["tuning_change_seconds"], d["break_count"], d["break_seconds"]))
    new_row = cur.fetchone()
    return new_row["id"] if isinstance(new_row, dict) else new_row[0]


def get_setlist(user_id: str) -> list:
    """Returns ordered list of song UUIDs in the user's default setlist."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            sid = _default_setlist_id(cur, user_id=user_id)
            if not sid:
                return []
            cur.execute(
                "SELECT song_id FROM setlist_songs WHERE setlist_id = %s ORDER BY position",
                (sid,)
            )
            return [str(r[0]) for r in cur.fetchall()]
    finally:
        put_conn(conn)


def save_setlist(user_id: str, entries: list) -> None:
    """entries: [{"song_id": uuid, "position": int, "plays": int}, ...] — full replace."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            sid = _default_setlist_id(cur, user_id=user_id, create=True)
            cur.execute("DELETE FROM setlist_songs WHERE setlist_id = %s", (sid,))
            if entries:
                cur.executemany(
                    "INSERT INTO setlist_songs (setlist_id, song_id, position, plays) VALUES (%s, %s, %s, %s)",
                    [(sid, e["song_id"], e["position"], e.get("plays", 1)) for e in entries]
                )
            cur.execute("UPDATE setlists SET updated_at = now() WHERE id = %s", (sid,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def get_band_setlist(band_id: str) -> list:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            sid = _default_setlist_id(cur, band_id=band_id)
            if not sid:
                return []
            cur.execute(
                "SELECT song_id FROM setlist_songs WHERE setlist_id = %s ORDER BY position",
                (sid,)
            )
            return [str(r[0]) for r in cur.fetchall()]
    finally:
        put_conn(conn)


def save_band_setlist(band_id: str, entries: list) -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            sid = _default_setlist_id(cur, band_id=band_id, create=True)
            cur.execute("DELETE FROM setlist_songs WHERE setlist_id = %s", (sid,))
            if entries:
                cur.executemany(
                    "INSERT INTO setlist_songs (setlist_id, song_id, position, plays) VALUES (%s, %s, %s, %s)",
                    [(sid, e["song_id"], e["position"], e.get("plays", 1)) for e in entries]
                )
            cur.execute("UPDATE setlists SET updated_at = now() WHERE id = %s", (sid,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


# ── Public read-only sharing ─────────────────────────────────────────────────────
#
# Every setlist carries a permanent, unguessable share_token (like bands.invite_token).
# get_setlist_share_token returns the token for a given setlist (caller resolves +
# validates ownership). get_shared_setlist resolves a token to a public, read-only
# payload — only display fields, no proposals/votes/emails/library songs.

def get_setlist_share_token(setlist_id: str) -> str:
    """Return the permanent share token for a specific setlist."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT share_token FROM setlists WHERE id = %s", (setlist_id,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        put_conn(conn)


# ── Multi-setlist helpers ─────────────────────────────────────────────────────

def _owns_setlist(cur, setlist_id: str, *, user_id=None, band_id=None) -> bool:
    """Return True if the given owner owns setlist_id."""
    if band_id:
        cur.execute("SELECT 1 FROM setlists WHERE id = %s AND band_id = %s", (setlist_id, band_id))
    else:
        cur.execute("SELECT 1 FROM setlists WHERE id = %s AND user_id = %s", (setlist_id, user_id))
    return cur.fetchone() is not None


def _settings_from_row(row) -> dict:
    return {c: row[c] for c in _SETTINGS_COLS}


def list_setlists(*, user_id=None, band_id=None) -> list:
    """Return all setlists for the owner, oldest first. Each dict includes settings."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if band_id:
                cur.execute(
                    "SELECT * FROM setlists WHERE band_id = %s ORDER BY created_at",
                    (band_id,)
                )
            else:
                cur.execute(
                    "SELECT * FROM setlists WHERE user_id = %s ORDER BY created_at",
                    (user_id,)
                )
            rows = cur.fetchall()
        result = []
        for i, r in enumerate(rows):
            result.append({
                "id": str(r["id"]),
                "name": r["name"],
                "is_default": i == 0,
                "settings": _settings_from_row(r),
            })
        return result
    finally:
        put_conn(conn)


def create_setlist(name: str, *, user_id=None, band_id=None) -> dict:
    """Create a new named setlist, seeded from band defaults (or SOLO_DEFAULT_SETTINGS)."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if band_id:
                cur.execute("""
                    INSERT INTO setlists (name, band_id,
                        target_seconds, warn_seconds, song_buffer_seconds,
                        tuning_change_seconds, break_count, break_seconds)
                    SELECT %s, %s,
                        b.default_target_seconds, b.default_warn_seconds,
                        b.default_song_buffer_seconds, b.default_tuning_change_seconds,
                        b.default_break_count, b.default_break_seconds
                    FROM bands b WHERE b.id = %s
                    RETURNING *
                """, (name, band_id, band_id))
            else:
                d = SOLO_DEFAULT_SETTINGS
                cur.execute("""
                    INSERT INTO setlists (name, user_id,
                        target_seconds, warn_seconds, song_buffer_seconds,
                        tuning_change_seconds, break_count, break_seconds)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
                """, (name, user_id, d["target_seconds"], d["warn_seconds"],
                      d["song_buffer_seconds"], d["tuning_change_seconds"],
                      d["break_count"], d["break_seconds"]))
            row = cur.fetchone()
        conn.commit()
        return {
            "id": str(row["id"]),
            "name": row["name"],
            "is_default": False,
            "settings": _settings_from_row(row),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def get_shared_setlist(token: str):
    """Public read-only lookup by share token.

    Returns {"name", "ownerName", "settings", "songs": [...]} or None if the token
    is unknown. Songs carry only display fields (no proposals/votes/owner identity)."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM setlists WHERE share_token = %s", (token,))
            sl = cur.fetchone()
            if not sl:
                return None

            # Friendly owner label: band name, or the solo owner's display name.
            if sl["band_id"]:
                cur.execute("SELECT name FROM bands WHERE id = %s", (sl["band_id"],))
                row = cur.fetchone()
                owner_name = row["name"] if row else ""
            else:
                cur.execute("""
                    SELECT COALESCE(up.display_name, nu.name, nu.email) AS name
                    FROM neon_auth."user" nu
                    LEFT JOIN user_profiles up ON up.user_id = nu.id
                    WHERE nu.id = %s
                """, (sl["user_id"],))
                row = cur.fetchone()
                owner_name = row["name"] if row else ""

            # Ordered setlist songs — display fields only, plays from setlist_songs.
            cur.execute("""
                SELECT s.id, s.name, s.artist, s.duration_raw, s.duration_sec,
                       s.album_art, s.tuning, s.recorded_tuning, s.our_tuning, s.status,
                       s.spotify_url, s.youtube_link, ss.plays
                FROM setlist_songs ss
                JOIN songs s ON s.id = ss.song_id
                WHERE ss.setlist_id = %s
                ORDER BY ss.position
            """, (sl["id"],))
            songs = [_row_to_song(r) for r in cur.fetchall()]

        return {
            "name": sl["name"],
            "ownerName": owner_name,
            "settings": _settings_from_row(sl),
            "songs": songs,
        }
    finally:
        put_conn(conn)


def rename_setlist(setlist_id: str, name: str, *, user_id=None, band_id=None) -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if not _owns_setlist(cur, setlist_id, user_id=user_id, band_id=band_id):
                raise ValueError("not found")
            cur.execute(
                "UPDATE setlists SET name = %s, updated_at = now() WHERE id = %s",
                (name, setlist_id)
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def delete_setlist(setlist_id: str, *, user_id=None, band_id=None) -> str:
    """Delete a setlist. Raises ValueError if it's the owner's last one.
    Returns the id of the owner's new default (earliest remaining) setlist."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if not _owns_setlist(cur, setlist_id, user_id=user_id, band_id=band_id):
                raise ValueError("not found")
            if band_id:
                cur.execute(
                    "SELECT id FROM setlists WHERE band_id = %s ORDER BY created_at",
                    (band_id,)
                )
            else:
                cur.execute(
                    "SELECT id FROM setlists WHERE user_id = %s ORDER BY created_at",
                    (user_id,)
                )
            all_ids = [str(r[0]) for r in cur.fetchall()]
            if len(all_ids) <= 1:
                raise ValueError("cannot delete the last setlist")
            cur.execute("DELETE FROM setlists WHERE id = %s", (setlist_id,))
            remaining = [i for i in all_ids if i != setlist_id]
        conn.commit()
        return remaining[0]
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def update_setlist_settings(setlist_id: str, settings: dict, *, user_id=None, band_id=None) -> dict:
    """Partial-update timing settings for a setlist. Returns the updated settings dict."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if not _owns_setlist(cur, setlist_id, user_id=user_id, band_id=band_id):
                raise ValueError("not found")
            # Only update recognised keys; clamp non-negative; warn must be < target.
            cur.execute("SELECT * FROM setlists WHERE id = %s", (setlist_id,))
            current = dict(cur.fetchone())
            for key in _SETTINGS_COLS:
                if key in settings:
                    current[key] = max(0, int(settings[key]))
            current["warn_seconds"] = min(current["warn_seconds"], current["target_seconds"])
            cur.execute("""
                UPDATE setlists
                SET target_seconds = %s, warn_seconds = %s, song_buffer_seconds = %s,
                    tuning_change_seconds = %s, break_count = %s, break_seconds = %s,
                    updated_at = now()
                WHERE id = %s
            """, (current["target_seconds"], current["warn_seconds"],
                  current["song_buffer_seconds"], current["tuning_change_seconds"],
                  current["break_count"], current["break_seconds"], setlist_id))
        conn.commit()
        return _settings_from_row(current)
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def get_setlist_full(setlist_id: str) -> dict:
    """Return {id, name, settings, entries:[{song_id, plays}]} for any setlist."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM setlists WHERE id = %s", (setlist_id,))
            sl = cur.fetchone()
            if not sl:
                return None
            cur.execute(
                "SELECT song_id::text, plays FROM setlist_songs WHERE setlist_id = %s ORDER BY position",
                (setlist_id,)
            )
            entries = [{"song_id": r["song_id"], "plays": r["plays"]} for r in cur.fetchall()]
        return {
            "id": str(sl["id"]),
            "name": sl["name"],
            "settings": _settings_from_row(sl),
            "entries": entries,
        }
    finally:
        put_conn(conn)


def save_setlist_entries(setlist_id: str, entries: list) -> None:
    """Full replace of songs in a setlist. entries: [{song_id, position, plays}]."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM setlist_songs WHERE setlist_id = %s", (setlist_id,))
            if entries:
                cur.executemany(
                    "INSERT INTO setlist_songs (setlist_id, song_id, position, plays) VALUES (%s, %s, %s, %s)",
                    [(setlist_id, e["song_id"], e["position"], e.get("plays", 1)) for e in entries]
                )
            cur.execute("UPDATE setlists SET updated_at = now() WHERE id = %s", (setlist_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def set_band_time_defaults(band_id: str, defaults: dict) -> None:
    """Partial-update band-level default timing settings."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM bands WHERE id = %s", (band_id,))
            current = dict(cur.fetchone())
            for key in _SETTINGS_COLS:
                db_col = f"default_{key}"
                if key in defaults:
                    current[db_col] = max(0, int(defaults[key]))
            current[f"default_warn_seconds"] = min(
                current["default_warn_seconds"], current["default_target_seconds"]
            )
            cur.execute("""
                UPDATE bands
                SET default_target_seconds = %s, default_warn_seconds = %s,
                    default_song_buffer_seconds = %s, default_tuning_change_seconds = %s,
                    default_break_count = %s, default_break_seconds = %s
                WHERE id = %s
            """, (current["default_target_seconds"], current["default_warn_seconds"],
                  current["default_song_buffer_seconds"], current["default_tuning_change_seconds"],
                  current["default_break_count"], current["default_break_seconds"], band_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


# ── Tunings ────────────────────────────────────────────────────────────────────

def get_tunings(user_id: str) -> list:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tuning FROM user_tunings WHERE user_id = %s ORDER BY tuning",
                (user_id,)
            )
            custom = [r[0] for r in cur.fetchall()]
        return list(dict.fromkeys(DEFAULT_TUNINGS + custom))
    finally:
        put_conn(conn)


def add_tuning(user_id: str, tuning: str) -> None:
    if tuning in DEFAULT_TUNINGS:
        return
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_tunings (user_id, tuning) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (user_id, tuning)
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def delete_tuning(user_id: str, tuning: str) -> bool:
    if tuning in DEFAULT_TUNINGS:
        return False
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_tunings WHERE user_id = %s AND tuning = %s",
                (user_id, tuning)
            )
            deleted = cur.rowcount > 0
        conn.commit()
        return deleted
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


# ── Band ────────────────────────────────────────────────────────────────────────

def get_user_band(user_id: str):
    """Returns band info dict with members, or None if user not in a band.
    NOTE: still single-band (LIMIT 1) until the multi-band UI lands; the schema
    already allows a user to be in several bands."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT b.id, b.name, b.invite_token, b.approval_factor,
                       b.default_target_seconds, b.default_warn_seconds,
                       b.default_song_buffer_seconds, b.default_tuning_change_seconds,
                       b.default_break_count, b.default_break_seconds,
                       bm.role
                FROM bands b
                JOIN band_members bm ON bm.band_id = b.id
                WHERE bm.user_id = %s
                LIMIT 1
            """, (user_id,))
            band_row = cur.fetchone()
            if not band_row:
                return None

            cur.execute("""
                SELECT bm.user_id::text, bm.role, bm.joined_at,
                       COALESCE(up.display_name, nu.name, nu.email) AS name,
                       nu.email,
                       COALESCE(up.roles, '{}') AS roles
                FROM band_members bm
                JOIN neon_auth."user" nu ON nu.id = bm.user_id
                LEFT JOIN user_profiles up ON up.user_id = bm.user_id
                WHERE bm.band_id = %s
                ORDER BY bm.joined_at
            """, (band_row["id"],))
            members = [dict(r) for r in cur.fetchall()]

            return {
                "id": str(band_row["id"]),
                "name": band_row["name"],
                "invite_token": band_row["invite_token"],
                "approval_factor": float(band_row["approval_factor"]),
                "time_defaults": {
                    "target_seconds":        band_row["default_target_seconds"],
                    "warn_seconds":          band_row["default_warn_seconds"],
                    "song_buffer_seconds":   band_row["default_song_buffer_seconds"],
                    "tuning_change_seconds": band_row["default_tuning_change_seconds"],
                    "break_count":           band_row["default_break_count"],
                    "break_seconds":         band_row["default_break_seconds"],
                },
                "role": band_row["role"],
                "members": members,
            }
    finally:
        put_conn(conn)


def create_band(user_id: str, name: str) -> dict:
    """Create a new band with the user as admin. Returns band dict."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO bands (name) VALUES (%s) RETURNING *",
                (name,)
            )
            band = cur.fetchone()
            band_id = band["id"]

            cur.execute(
                "INSERT INTO band_members (band_id, user_id, role) VALUES (%s, %s, 'admin')",
                (band_id, user_id)
            )
        conn.commit()
        return {
            "id": str(band["id"]),
            "name": band["name"],
            "invite_token": band["invite_token"],
            "role": "admin",
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def join_band(user_id: str, invite_token: str) -> dict:
    """Join a band by invite token. Raises ValueError for invalid token or full band."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM bands WHERE invite_token = %s", (invite_token,))
            band = cur.fetchone()
            if not band:
                raise ValueError("Invalid invite token")

            band_id = band["id"]

            cur.execute(
                "SELECT 1 FROM band_members WHERE band_id = %s AND user_id = %s",
                (band_id, user_id)
            )
            if cur.fetchone():
                return {"id": str(band_id), "name": band["name"], "already_member": True}

            cur.execute("SELECT COUNT(*) AS cnt FROM band_members WHERE band_id = %s", (band_id,))
            if cur.fetchone()["cnt"] >= 24:
                raise ValueError("Band is full (max 24 members)")

            cur.execute(
                "INSERT INTO band_members (band_id, user_id, role) VALUES (%s, %s, 'member')",
                (band_id, user_id)
            )
        conn.commit()
        return {"id": str(band_id), "name": band["name"]}
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def migrate_songs_to_band(user_id: str, band_id: str) -> None:
    """
    Adopt a user's personal songs into the band library by changing ownership
    (band_id set, user_id cleared — required by the one-owner CHECK). Songs that
    would duplicate an existing band song (same external_id) are left personal to
    avoid a duplicate-guard violation. Also copies the user's default setlist into
    the band's default setlist.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # 1. Capture the user's current default-setlist ordering BEFORE reassigning,
            #    so we can rebuild it on the band side (song UUIDs are unchanged).
            user_sid = _default_setlist_id(cur, user_id=user_id)
            setlist_rows = []
            if user_sid:
                cur.execute(
                    "SELECT song_id, position, plays FROM setlist_songs "
                    "WHERE setlist_id = %s ORDER BY position", (user_sid,)
                )
                setlist_rows = cur.fetchall()

            # 2. Move non-conflicting personal songs to the band.
            cur.execute("""
                UPDATE songs SET band_id = %s, user_id = NULL,
                                 added_by = COALESCE(added_by, %s)
                WHERE user_id = %s
                  AND (external_id IS NULL OR NOT EXISTS (
                        SELECT 1 FROM songs b
                        WHERE b.band_id = %s AND b.external_id = songs.external_id))
            """, (band_id, user_id, user_id, band_id))

            # 3. Copy the captured setlist into the band's default setlist, keeping only
            #    songs that actually made it to the band (i.e. now owned by this band).
            if setlist_rows:
                band_sid = _default_setlist_id(cur, band_id=band_id, create=True)
                for sr in setlist_rows:
                    song_id, position, plays = sr[0], sr[1], sr[2]
                    cur.execute(
                        "SELECT 1 FROM songs WHERE id = %s AND band_id = %s",
                        (song_id, band_id)
                    )
                    if cur.fetchone():
                        cur.execute("""
                            INSERT INTO setlist_songs (setlist_id, song_id, position, plays)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (setlist_id, song_id) DO NOTHING
                        """, (band_sid, song_id, position, plays))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def create_proposal(band_id: str, song_id: str, proposed_by: str) -> dict:
    """Create a proposal, auto-vote 5 (Love it) for proposer, and notify other members.
    `song_id` must be the song's UUID."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO song_proposals (band_id, song_id, proposed_by, score)
                VALUES (%s, %s, %s, 5)
                RETURNING *
            """, (band_id, song_id, proposed_by))
            proposal = cur.fetchone()
            proposal_id = proposal["id"]

            cur.execute(
                "INSERT INTO song_votes (proposal_id, user_id, vote) VALUES (%s, %s, '5')",
                (proposal_id, proposed_by)
            )

            cur.execute("""
                INSERT INTO notifications (user_id, proposal_id, type)
                SELECT bm.user_id, %s, 'new_proposal'
                FROM band_members bm
                LEFT JOIN notification_prefs np ON np.user_id = bm.user_id
                WHERE bm.band_id = %s AND bm.user_id != %s
                  AND COALESCE(np.new_proposal, true)
            """, (proposal_id, band_id, proposed_by))
        conn.commit()
        return dict(proposal)
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def cast_vote(proposal_id: str, user_id: str, vote: str, reason: str = None) -> dict:
    """Cast or update a vote (Likert 1–5). Works on pending and approved proposals."""
    if vote not in VOTE_POINTS:
        raise ValueError(f"Invalid vote: {vote}")

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT sp.id, sp.band_id, sp.song_id, sp.status,
                       COUNT(bm.user_id) AS band_size,
                       b.approval_factor
                FROM song_proposals sp
                JOIN band_members bm ON bm.band_id = sp.band_id
                JOIN bands b ON b.id = sp.band_id
                WHERE sp.id = %s AND sp.status IN ('pending', 'approved')
                GROUP BY sp.id, b.approval_factor
            """, (proposal_id,))
            proposal = cur.fetchone()
            if not proposal:
                raise ValueError("Proposal not found or not open for voting")

            band_id   = proposal["band_id"]
            band_size = int(proposal["band_size"])
            approval_factor = float(proposal["approval_factor"])
            approval_threshold = math.ceil(band_size * approval_factor)

            cur.execute("""
                INSERT INTO song_votes (proposal_id, user_id, vote, reason)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (proposal_id, user_id)
                DO UPDATE SET vote = EXCLUDED.vote, reason = EXCLUDED.reason
            """, (proposal_id, user_id, vote, reason or None))

            cur.execute("""
                SELECT vote, COUNT(*) AS cnt
                FROM song_votes WHERE proposal_id = %s
                GROUP BY vote
            """, (proposal_id,))
            vote_counts = {r["vote"]: int(r["cnt"]) for r in cur.fetchall()}

            score      = sum(VOTE_POINTS.get(v, 0) * cnt for v, cnt in vote_counts.items())
            votes_cast = sum(vote_counts.values())
            remaining  = band_size - votes_cast
            max_possible = score + remaining * 5

            cur.execute("UPDATE song_proposals SET score = %s WHERE id = %s", (score, proposal_id))

            new_status = proposal["status"]

            if proposal["status"] == "pending":
                if score >= approval_threshold:
                    new_status = "approved"
                    cur.execute(
                        "UPDATE songs SET status = 'Learning' WHERE id = %s AND band_id = %s",
                        (proposal["song_id"], band_id)
                    )
                    _auto_add_to_setlist(cur, str(band_id), proposal["song_id"])
                elif max_possible < approval_threshold:
                    new_status = "rejected"
                    cur.execute(
                        "UPDATE songs SET status = 'Archived' WHERE id = %s AND band_id = %s",
                        (proposal["song_id"], band_id)
                    )
                    _notify_failure(cur, band_id, proposal_id, proposal["song_id"],
                                    vote_counts, score, band_size, "proposal_failed")

            elif proposal["status"] == "approved":
                if max_possible < approval_threshold:
                    new_status = "archived"
                    cur.execute(
                        "UPDATE songs SET status = 'Archived' WHERE id = %s AND band_id = %s",
                        (proposal["song_id"], band_id)
                    )
                    _notify_failure(cur, band_id, proposal_id, proposal["song_id"],
                                    vote_counts, score, band_size, "song_archived")

            if new_status != proposal["status"]:
                cur.execute(
                    "UPDATE song_proposals SET status = %s WHERE id = %s",
                    (new_status, proposal_id)
                )

            cur.execute("""
                UPDATE notifications SET read = true
                WHERE user_id = %s AND proposal_id = %s
            """, (user_id, proposal_id))

        conn.commit()
        return {"id": str(proposal_id), "status": new_status, "score": score}
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def _notify_failure(cur, band_id, proposal_id, song_id, vote_counts, score, band_size, notif_type):
    """Send a failure/demotion notification to all band members with the vote breakdown."""
    cur.execute("""
        SELECT sv.vote, sv.reason,
               COALESCE(up.display_name, nu.name, nu.email) AS name
        FROM song_votes sv
        JOIN neon_auth."user" nu ON nu.id = sv.user_id
        LEFT JOIN user_profiles up ON up.user_id = sv.user_id
        WHERE sv.proposal_id = %s
    """, (proposal_id,))
    votes_detail = [
        {"name": r["name"], "vote": r["vote"], "reason": r["reason"]}
        for r in cur.fetchall()
    ]
    details = json.dumps({
        "score":     score,
        "max_score": band_size * 5,
        "votes":     votes_detail,
    })
    cur.execute("""
        INSERT INTO notifications (user_id, proposal_id, type, details)
        SELECT bm.user_id, %s, %s, %s::jsonb
        FROM band_members bm
        LEFT JOIN notification_prefs np ON np.user_id = bm.user_id
        WHERE bm.band_id = %s
          AND COALESCE(
                CASE %s
                    WHEN 'proposal_failed' THEN np.proposal_failed
                    WHEN 'song_archived'   THEN np.song_archived
                END, true)
    """, (proposal_id, notif_type, details, band_id, notif_type))


def _auto_add_to_setlist(cur, band_id: str, song_id: str) -> None:
    """Append an approved song to the band's default setlist."""
    sid = _default_setlist_id(cur, band_id=band_id, create=True)
    cur.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 AS next_pos FROM setlist_songs WHERE setlist_id = %s",
        (sid,)
    )
    pos_row = cur.fetchone()
    next_pos = pos_row["next_pos"] if isinstance(pos_row, dict) else pos_row[0]
    cur.execute("""
        INSERT INTO setlist_songs (setlist_id, song_id, position, plays)
        VALUES (%s, %s, %s, 1)
        ON CONFLICT (setlist_id, song_id) DO NOTHING
    """, (sid, song_id, next_pos))


def get_pending_proposals(band_id: str, user_id: str) -> list:
    """Proposals the user hasn't voted on yet (excluding their own proposals)."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT sp.id, sp.song_id::text, sp.proposed_by::text, sp.score, sp.created_at,
                       COALESCE(up.display_name, nu.name, nu.email) AS proposer_name,
                       s.name AS song_name, s.artist, s.duration_raw,
                       s.album_art, s.spotify_url, s.duration_sec
                FROM song_proposals sp
                JOIN neon_auth."user" nu ON nu.id = sp.proposed_by
                LEFT JOIN user_profiles up ON up.user_id = sp.proposed_by
                JOIN songs s ON s.id = sp.song_id AND s.band_id = %s
                WHERE sp.band_id = %s
                  AND sp.status IN ('pending', 'approved')
                  AND sp.proposed_by != %s
                  AND NOT EXISTS (
                      SELECT 1 FROM song_votes sv
                      WHERE sv.proposal_id = sp.id AND sv.user_id = %s
                  )
                ORDER BY sp.created_at
            """, (band_id, band_id, user_id, user_id))
            proposals = cur.fetchall()

            result = []
            for p in proposals:
                cur.execute("""
                    SELECT sv.vote, sv.reason,
                           COALESCE(up2.display_name, nu2.name, nu2.email) AS name
                    FROM song_votes sv
                    JOIN neon_auth."user" nu2 ON nu2.id = sv.user_id
                    LEFT JOIN user_profiles up2 ON up2.user_id = sv.user_id
                    WHERE sv.proposal_id = %s
                """, (p["id"],))
                votes = [{"vote": r["vote"], "name": r["name"], "reason": r["reason"]} for r in cur.fetchall()]

                result.append({
                    "id": str(p["id"]),
                    "song_id": p["song_id"],
                    "proposed_by": p["proposed_by"],
                    "proposer_name": p["proposer_name"],
                    "score": p["score"],
                    "song": {
                        "name": p["song_name"],
                        "artist": p["artist"],
                        "duration_raw": p["duration_raw"],
                        "album_art": p["album_art"],
                        "spotify_url": p["spotify_url"],
                    },
                    "votes": votes,
                })
            return result
    finally:
        put_conn(conn)


def get_notifications(user_id: str) -> dict:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT n.id, n.proposal_id, n.type, n.read, n.created_at,
                       s.name AS song_name, s.artist,
                       nu.name AS proposer_name
                FROM notifications n
                JOIN song_proposals sp ON sp.id = n.proposal_id
                JOIN songs s ON s.id = sp.song_id
                JOIN neon_auth."user" nu ON nu.id = sp.proposed_by
                WHERE n.user_id = %s
                ORDER BY n.created_at DESC
                LIMIT 50
            """, (user_id,))
            rows = cur.fetchall()

        unread = sum(1 for r in rows if not r["read"])
        notifications = [{
            "id": str(r["id"]),
            "proposal_id": str(r["proposal_id"]),
            "type": r["type"],
            "read": r["read"],
            "song_name": r["song_name"],
            "artist": r["artist"],
            "proposer_name": r["proposer_name"],
        } for r in rows]

        return {"unread": unread, "notifications": notifications}
    finally:
        put_conn(conn)


def mark_notifications_read(user_id: str, proposal_ids: list) -> None:
    if not proposal_ids:
        return
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE notifications SET read = true
                WHERE user_id = %s AND proposal_id = ANY(%s)
            """, (user_id, proposal_ids))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


# ── User Profile & Preferences ──────────────────────────────────────────────────

def get_profile(user_id: str) -> dict:
    """Return the user's display name, roles, and notification prefs (defaults if no rows)."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT display_name, roles FROM user_profiles WHERE user_id = %s",
                (user_id,)
            )
            prof = cur.fetchone()
            cur.execute(
                "SELECT new_proposal, proposal_failed, song_archived "
                "FROM notification_prefs WHERE user_id = %s",
                (user_id,)
            )
            prefs = cur.fetchone()
        return {
            "display_name": (prof or {}).get("display_name"),
            "roles": list((prof or {}).get("roles") or []),
            "notif_prefs": {
                "new_proposal":    (prefs or {}).get("new_proposal", True),
                "proposal_failed": (prefs or {}).get("proposal_failed", True),
                "song_archived":   (prefs or {}).get("song_archived", True),
            },
        }
    finally:
        put_conn(conn)


def upsert_profile(user_id: str, display_name, roles: list) -> None:
    display_name = (display_name or "").strip() or None
    roles = [str(r).strip() for r in (roles or []) if str(r).strip()]
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_profiles (user_id, display_name, roles, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (user_id) DO UPDATE SET
                  display_name = EXCLUDED.display_name,
                  roles        = EXCLUDED.roles,
                  updated_at   = now()
            """, (user_id, display_name, roles))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def update_notif_prefs(user_id: str, prefs: dict) -> None:
    """Upsert the three notification toggles (missing keys default to True)."""
    np = {
        "new_proposal":    bool(prefs.get("new_proposal", True)),
        "proposal_failed": bool(prefs.get("proposal_failed", True)),
        "song_archived":   bool(prefs.get("song_archived", True)),
    }
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO notification_prefs
                    (user_id, new_proposal, proposal_failed, song_archived)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                  new_proposal    = EXCLUDED.new_proposal,
                  proposal_failed = EXCLUDED.proposal_failed,
                  song_archived   = EXCLUDED.song_archived
            """, (user_id, np["new_proposal"], np["proposal_failed"], np["song_archived"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


# ── Band Management ──────────────────────────────────────────────────────────────

def rename_band(band_id: str, name: str) -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE bands SET name = %s WHERE id = %s", (name, band_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def set_approval_factor(band_id: str, factor: float) -> None:
    factor = max(1.0, min(5.0, float(factor)))
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE bands SET approval_factor = %s WHERE id = %s", (factor, band_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def regenerate_invite(band_id: str) -> str:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bands SET invite_token = gen_random_uuid()::text "
                "WHERE id = %s RETURNING invite_token",
                (band_id,)
            )
            token = cur.fetchone()[0]
        conn.commit()
        return token
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def promote_member(band_id: str, target_user_id: str) -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE band_members SET role = 'admin' WHERE band_id = %s AND user_id = %s",
                (band_id, target_user_id)
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def remove_member(band_id: str, target_user_id: str) -> None:
    """Remove a member. Refuses to remove the last remaining admin."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT role FROM band_members WHERE band_id = %s AND user_id = %s",
                (band_id, target_user_id)
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("That member is not in the band")
            if row["role"] == "admin":
                cur.execute(
                    "SELECT COUNT(*) AS c FROM band_members WHERE band_id = %s AND role = 'admin'",
                    (band_id,)
                )
                if cur.fetchone()["c"] <= 1:
                    raise ValueError("Cannot remove the only admin")
            cur.execute(
                "DELETE FROM band_members WHERE band_id = %s AND user_id = %s",
                (band_id, target_user_id)
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def leave_band(user_id: str, band_id: str) -> None:
    """Leave the band. Blocks a sole admin while members remain; deletes the band if last to leave."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT role FROM band_members WHERE band_id = %s AND user_id = %s",
                (band_id, user_id)
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("You are not a member of this band")

            cur.execute("SELECT COUNT(*) AS c FROM band_members WHERE band_id = %s", (band_id,))
            total = cur.fetchone()["c"]

            if total <= 1:
                cur.execute("DELETE FROM bands WHERE id = %s", (band_id,))
                conn.commit()
                return

            if row["role"] == "admin":
                cur.execute(
                    "SELECT COUNT(*) AS c FROM band_members WHERE band_id = %s AND role = 'admin'",
                    (band_id,)
                )
                if cur.fetchone()["c"] <= 1:
                    raise ValueError(
                        "You are the only admin. Promote someone to admin first, or delete the band."
                    )

            cur.execute(
                "DELETE FROM band_members WHERE band_id = %s AND user_id = %s",
                (band_id, user_id)
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def delete_band(band_id: str) -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bands WHERE id = %s", (band_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)
