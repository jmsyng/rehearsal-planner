import json
import math
import os
import psycopg2
from psycopg2.extras import RealDictCursor

DEFAULT_TUNINGS = ['E standard', 'Eb', 'Drop D', 'Drop C#']

# 5-point Likert scale — stored as strings "1"–"5"
VOTE_POINTS = {"5": 5, "4": 4, "3": 3, "2": 2, "1": 1}
# Approval threshold = math.ceil(band_size * 3.5), computed dynamically in cast_vote


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


def upsert_song(user_id: str, song: dict, band_id: str = None) -> dict:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO songs
                  (id, user_id, name, duration_raw, duration_sec, plays,
                   artist, status, tuning, recorded_tuning, our_tuning,
                   album_art, spotify_url, youtube_link, band_id)
                VALUES
                  (%(id)s, %(user_id)s, %(name)s, %(duration_raw)s, %(duration_sec)s, %(plays)s,
                   %(artist)s, %(status)s, %(tuning)s, %(recorded_tuning)s, %(our_tuning)s,
                   %(album_art)s, %(spotify_url)s, %(youtube_link)s, %(band_id)s)
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
                  youtube_link    = EXCLUDED.youtube_link,
                  band_id         = COALESCE(songs.band_id, EXCLUDED.band_id)
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
                "band_id": band_id,
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


# ── Band ────────────────────────────────────────────────────────────────────────

def get_user_band(user_id: str):
    """Returns band info dict with members, or None if user not in a band."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT b.id, b.name, b.invite_token, b.approval_factor, bm.role
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

            cur.execute("SELECT COUNT(*) as cnt FROM band_members WHERE band_id = %s", (band_id,))
            count = cur.fetchone()["cnt"]
            if count >= 4:
                raise ValueError("Band is full (max 4 members)")

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
    """Adopt all of a user's personal songs into the band library."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE songs SET band_id = %s WHERE user_id = %s AND band_id IS NULL",
                (band_id, user_id)
            )
            cur.execute("""
                INSERT INTO band_set_list_songs (band_id, song_id, position, plays)
                SELECT %s, song_id, position, plays
                FROM set_list_songs
                WHERE user_id = %s
                ON CONFLICT (band_id, song_id) DO NOTHING
            """, (band_id, user_id))
        conn.commit()
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
                LEFT JOIN neon_auth."user" nu ON nu.id = s.user_id
                LEFT JOIN user_profiles up ON up.user_id = s.user_id
                LEFT JOIN song_proposals sp
                       ON sp.song_id = s.id
                      AND sp.band_id = %(band_id)s
                      AND sp.status  IN ('pending', 'approved')
                LEFT JOIN song_votes sv_me
                       ON sv_me.proposal_id = sp.id
                      AND sv_me.user_id = %(user_id)s
                WHERE s.band_id = %(band_id)s
                ORDER BY s.created_at
            """, {"band_id": band_id, "user_id": user_id})
            rows = cur.fetchall()
        return [_row_to_band_song(r) for r in rows]
    finally:
        put_conn(conn)


def get_band_setlist(band_id: str) -> list:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT song_id FROM band_set_list_songs WHERE band_id = %s ORDER BY position",
                (band_id,)
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        put_conn(conn)


def save_band_setlist(band_id: str, entries: list) -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM band_set_list_songs WHERE band_id = %s", (band_id,))
            if entries:
                cur.executemany(
                    "INSERT INTO band_set_list_songs (band_id, song_id, position, plays) VALUES (%s, %s, %s, %s)",
                    [(band_id, e["song_id"], e["position"], e.get("plays", 1)) for e in entries]
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def create_proposal(band_id: str, song_id: str, proposed_by: str) -> dict:
    """Create a proposal, auto-vote 5 (Love it) for proposer, and notify other members."""
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
            # Accept votes on pending OR approved proposals
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

            # Upsert the vote (with optional reason)
            cur.execute("""
                INSERT INTO song_votes (proposal_id, user_id, vote, reason)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (proposal_id, user_id)
                DO UPDATE SET vote = EXCLUDED.vote, reason = EXCLUDED.reason
            """, (proposal_id, user_id, vote, reason or None))

            # Recompute score
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

            new_status = proposal["status"]  # default: unchanged

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

            # Mark related notifications read for this user
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
    # Fetch full vote detail (name + vote + reason) for the notification payload
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
    cur.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 FROM band_set_list_songs WHERE band_id = %s",
        (band_id,)
    )
    next_pos = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO band_set_list_songs (band_id, song_id, position, plays)
        VALUES (%s, %s, %s, 1)
        ON CONFLICT (band_id, song_id) DO NOTHING
    """, (band_id, song_id, next_pos))


def get_pending_proposals(band_id: str, user_id: str) -> list:
    """Proposals the user hasn't voted on yet (excluding their own proposals)."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT sp.id, sp.song_id, sp.proposed_by::text, sp.score, sp.created_at,
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
                # Last member leaving — remove the band entirely (cascades).
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
