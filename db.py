import os
import psycopg2
from psycopg2.extras import RealDictCursor

DEFAULT_TUNINGS = ['E standard', 'Eb', 'Drop D', 'Drop C#']


def get_conn():
    """Open a fresh Postgres connection. Recommended for Neon (serverless)."""
    return psycopg2.connect(os.environ["DATABASE_URL"])


def put_conn(conn):
    """Close the connection (no pooling). Safe to call even if already closed."""
    try:
        conn.close()
    except Exception:
        pass


# ── Songs ──────────────────────────────────────────────────────────────────────

def get_songs(user_id: str) -> list:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM songs WHERE user_id = %s ORDER BY created_at",
                (user_id,)
            )
            rows = cur.fetchall()
        return [_row_to_song(r) for r in rows]
    finally:
        put_conn(conn)


def upsert_song(user_id: str, song: dict) -> dict:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO songs
                  (id, user_id, name, duration_raw, duration_sec, plays,
                   artist, status, tuning, recorded_tuning, our_tuning,
                   album_art, spotify_url, youtube_link)
                VALUES
                  (%(id)s, %(user_id)s, %(name)s, %(duration_raw)s, %(duration_sec)s, %(plays)s,
                   %(artist)s, %(status)s, %(tuning)s, %(recorded_tuning)s, %(our_tuning)s,
                   %(album_art)s, %(spotify_url)s, %(youtube_link)s)
                ON CONFLICT (id, user_id) DO UPDATE SET
                  name            = EXCLUDED.name,
                  duration_raw    = EXCLUDED.duration_raw,
                  duration_sec    = EXCLUDED.duration_sec,
                  plays           = EXCLUDED.plays,
                  artist          = EXCLUDED.artist,
                  status          = EXCLUDED.status,
                  tuning          = EXCLUDED.tuning,
                  recorded_tuning = EXCLUDED.recorded_tuning,
                  our_tuning      = EXCLUDED.our_tuning,
                  album_art       = EXCLUDED.album_art,
                  spotify_url     = EXCLUDED.spotify_url,
                  youtube_link    = EXCLUDED.youtube_link
                RETURNING *
            """, {
                "id": song["id"],
                "user_id": user_id,
                "name": song["name"],
                "duration_raw": song.get("duration_raw", ""),
                "duration_sec": song.get("duration_seconds", 0),
                "plays": song.get("plays", 1),
                "artist": (song.get("extra") or {}).get("Artist", ""),
                "status": (song.get("extra") or {}).get("Status", "For Consideration"),
                "tuning": (song.get("extra") or {}).get("Tuning"),
                "recorded_tuning": (song.get("extra") or {}).get("RecordedTuning"),
                "our_tuning": (song.get("extra") or {}).get("OurTuning"),
                "album_art": (song.get("extra") or {}).get("albumArt"),
                "spotify_url": (song.get("extra") or {}).get("spotifyUrl"),
                "youtube_link": (song.get("extra") or {}).get("YouTubeLink"),
            })
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


def _row_to_song(row) -> dict:
    """Convert a DB row to the JS-compatible song object shape."""
    return {
        "id": row["id"],
        "name": row["name"],
        "duration_raw": row["duration_raw"],
        "duration_seconds": row["duration_sec"],
        "plays": row["plays"],
        "extra": {
            "Artist": row["artist"],
            "Status": row["status"],
            "Tuning": row["tuning"],
            "RecordedTuning": row["recorded_tuning"],
            "OurTuning": row["our_tuning"],
            "albumArt": row["album_art"],
            "spotifyUrl": row["spotify_url"],
            "YouTubeLink": row["youtube_link"],
        }
    }


# ── Set List ───────────────────────────────────────────────────────────────────

def get_setlist(user_id: str) -> list:
    """Returns ordered list of song_ids."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT song_id FROM set_list_songs WHERE user_id = %s ORDER BY position",
                (user_id,)
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        put_conn(conn)


def save_setlist(user_id: str, entries: list) -> None:
    """
    entries: [{"song_id": str, "position": int, "plays": int}, ...]
    Fully replaces the user's set list.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO set_lists (user_id) VALUES (%s)
                ON CONFLICT (user_id) DO UPDATE SET updated_at = now()
            """, (user_id,))
            cur.execute("DELETE FROM set_list_songs WHERE user_id = %s", (user_id,))
            if entries:
                cur.executemany(
                    "INSERT INTO set_list_songs (user_id, song_id, position, plays) VALUES (%s, %s, %s, %s)",
                    [(user_id, e["song_id"], e["position"], e.get("plays", 1)) for e in entries]
                )
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
