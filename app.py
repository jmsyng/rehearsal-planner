from dotenv import load_dotenv
load_dotenv()

import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error
import base64
import time
from flask import Flask, jsonify, render_template, request, g

from auth import require_auth
import db

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

_config = {
    "spotify_client_id": os.environ.get("SPOTIFY_CLIENT_ID", ""),
    "spotify_client_secret": os.environ.get("SPOTIFY_CLIENT_SECRET", ""),
    "spotify_access_token": None,
    "spotify_token_expiry": 0,
    "youtube_api_key": os.environ.get("YOUTUBE_API_KEY", ""),
}


def parse_duration(s):
    """Parse mm:ss or m:ss string into total seconds. Returns 0 on failure."""
    s = str(s).strip()
    try:
        parts = s.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except (ValueError, IndexError):
        pass
    return 0


@app.route("/")
def index():
    return render_template(
        "index.html",
        neon_auth_base_url=os.environ.get("NEON_AUTH_BASE_URL", ""),
    )


# ── Local dev auth proxy ─────────────────────────────────────────────────────────
# Neon Auth sets its session cookie as SameSite=None; Secure, owned by the
# *.neon.tech origin. Relative to http://localhost that's a cross-site cookie, which
# modern browsers block — so the browser-side `/token` JWT exchange fails and local
# sign-in never completes. These routes do the cookie round-trip *server-side* (Python
# isn't subject to browser cross-site cookie rules), returning the JWT to the page.
# Localhost-only: refuse on Vercel or any non-loopback caller so it can't be used in prod.

def _is_local_dev():
    if os.environ.get("VERCEL"):
        return False
    return (request.remote_addr or "") in ("127.0.0.1", "::1", "localhost")


def _neon_session_cookie(set_cookie_headers):
    """Collapse Set-Cookie response headers into a single Cookie request header value."""
    return "; ".join(h.split(";", 1)[0] for h in (set_cookie_headers or []) if h)


@app.route("/api/dev/login", methods=["POST"])
def dev_login():
    if not _is_local_dev():
        return jsonify({"error": "not found"}), 404
    base = os.environ.get("NEON_AUTH_BASE_URL", "").rstrip("/")
    if not base:
        return jsonify({"error": "auth not configured"}), 500

    body = request.get_json(force=True) or {}
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    mode = body.get("mode") or "signin"
    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    endpoint = "/sign-up/email" if mode == "signup" else "/sign-in/email"
    payload = {"email": email, "password": password}
    if mode == "signup":
        payload["name"] = body.get("name") or email.split("@")[0]

    # Better Auth validates the Origin against its trusted-origins list and rejects
    # requests without one. The browser sets this automatically; for our server-side
    # call we forward the app's own origin (the same one the browser would send).
    origin = request.host_url.rstrip("/")

    # 1) Sign in / up — capture the session cookie from the response headers.
    try:
        req = urllib.request.Request(f"{base}{endpoint}", method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Origin", origin)
        with urllib.request.urlopen(req, json.dumps(payload).encode(), timeout=10) as resp:
            cookie_header = _neon_session_cookie(resp.headers.get_all("Set-Cookie"))
            signin_data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())
            msg = err.get("message") or err.get("error") or err.get("code") or "Authentication failed"
        except Exception:
            msg = "Authentication failed"
        return jsonify({"error": msg}), (e.code if e.code in (400, 401, 409) else 401)
    except Exception:
        return jsonify({"error": "Could not reach auth server"}), 502

    if not cookie_header:
        return jsonify({"error": "Auth server returned no session cookie"}), 502

    # 2) Exchange the session cookie for a JWT, server-side.
    jwt = _exchange_session_for_jwt(base, cookie_header, origin)
    if not jwt:
        return jsonify({"error": "JWT exchange failed"}), 502

    email_out = (signin_data.get("user") or {}).get("email") or email
    return jsonify({"token": jwt, "email": email_out, "devSession": cookie_header})


@app.route("/api/dev/token", methods=["POST"])
def dev_token():
    """Refresh the 15-min JWT locally by replaying the stored dev session cookie."""
    if not _is_local_dev():
        return jsonify({"error": "not found"}), 404
    base = os.environ.get("NEON_AUTH_BASE_URL", "").rstrip("/")
    cookie_header = (request.get_json(force=True) or {}).get("devSession") or ""
    if not base or not cookie_header:
        return jsonify({"error": "missing session"}), 400
    jwt = _exchange_session_for_jwt(base, cookie_header, request.host_url.rstrip("/"))
    if not jwt:
        return jsonify({"error": "JWT exchange failed"}), 401
    return jsonify({"token": jwt})


def _exchange_session_for_jwt(base, cookie_header, origin):
    try:
        treq = urllib.request.Request(f"{base}/token", method="GET")
        treq.add_header("Cookie", cookie_header)
        treq.add_header("Origin", origin)
        with urllib.request.urlopen(treq, timeout=10) as tresp:
            return json.loads(tresp.read().decode()).get("token")
    except Exception:
        return None


# ── Songs API ──────────────────────────────────────────────────────────────────

@app.route("/api/songs", methods=["GET"])
@require_auth
def api_get_songs():
    band = db.get_user_band(g.user_id)
    if band:
        return jsonify(db.get_band_songs(band["id"], g.user_id))
    return jsonify(db.get_songs(g.user_id))


@app.route("/api/songs", methods=["POST"])
@require_auth
def api_add_song():
    song = request.get_json(force=True)
    if not song or not song.get("name"):
        return jsonify({"error": "name is required"}), 400
    band = db.get_user_band(g.user_id)
    band_id = band["id"] if band else None
    saved = db.upsert_song(g.user_id, song, band_id=band_id)
    if band_id:
        db.create_proposal(band_id, saved["id"], g.user_id)
        # Return the enriched band song so the frontend gets proposal metadata immediately
        for s in db.get_band_songs(band_id, g.user_id):
            if s["id"] == saved["id"]:
                return jsonify(s), 201
    return jsonify(saved), 201


@app.route("/api/songs/<song_id>", methods=["PUT"])
@require_auth
def api_update_song(song_id):
    song = request.get_json(force=True)
    song["id"] = song_id
    band = db.get_user_band(g.user_id)
    saved = db.upsert_song(g.user_id, song, band_id=band["id"] if band else None)
    return jsonify(saved)


@app.route("/api/songs/<song_id>", methods=["DELETE"])
@require_auth
def api_delete_song(song_id):
    band = db.get_user_band(g.user_id)
    if band:
        deleted = db.delete_band_song(band["id"], song_id)
    else:
        deleted = db.delete_song(g.user_id, song_id)
    if not deleted:
        return jsonify({"error": "Song not found"}), 404
    return jsonify({"ok": True})


# ── Set List API ───────────────────────────────────────────────────────────────

def _resolve_setlist(band, user_id, setlist_id=None):
    """Resolve + validate a setlist_id for the current user/band.
    Returns (setlist_id, error_response) — one of the two will be None."""
    band_id = band["id"] if band else None
    if setlist_id:
        # Validate ownership.
        import psycopg2
        from psycopg2 import connect
        conn = db.get_conn()
        try:
            with conn.cursor() as cur:
                owned = db._owns_setlist(cur, setlist_id,
                                         user_id=None if band_id else user_id,
                                         band_id=band_id)
            db.put_conn(conn)
        except Exception:
            db.put_conn(conn)
            return None, (jsonify({"error": "invalid setlist_id"}), 400)
        if not owned:
            return None, (jsonify({"error": "Set list not found"}), 404)
        return setlist_id, None
    # No id provided — use the default (earliest) setlist, creating if needed.
    conn = db.get_conn()
    try:
        with conn.cursor() as cur:
            sid = db._default_setlist_id(cur, band_id=band_id,
                                          user_id=None if band_id else user_id,
                                          create=True)
        conn.commit()
        db.put_conn(conn)
    except Exception:
        conn.rollback()
        db.put_conn(conn)
        raise
    return str(sid), None


@app.route("/api/setlists", methods=["GET"])
@require_auth
def api_list_setlists():
    band = db.get_user_band(g.user_id)
    if band:
        return jsonify(db.list_setlists(band_id=band["id"]))
    return jsonify(db.list_setlists(user_id=g.user_id))


@app.route("/api/setlists", methods=["POST"])
@require_auth
def api_create_setlist():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    band = db.get_user_band(g.user_id)
    if band:
        sl = db.create_setlist(name, band_id=band["id"])
    else:
        sl = db.create_setlist(name, user_id=g.user_id)
    return jsonify(sl), 201


@app.route("/api/setlists/<setlist_id>", methods=["PATCH"])
@require_auth
def api_update_setlist(setlist_id):
    band = db.get_user_band(g.user_id)
    band_id = band["id"] if band else None
    user_id = None if band_id else g.user_id
    data = request.get_json(force=True) or {}
    try:
        if "name" in data:
            name = (data["name"] or "").strip()
            if not name:
                return jsonify({"error": "name is required"}), 400
            db.rename_setlist(setlist_id, name, band_id=band_id, user_id=user_id)
        if "settings" in data:
            db.update_setlist_settings(setlist_id, data["settings"],
                                       band_id=band_id, user_id=user_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    sl = db.get_setlist_full(setlist_id)
    return jsonify(sl)


@app.route("/api/setlists/<setlist_id>", methods=["DELETE"])
@require_auth
def api_delete_setlist(setlist_id):
    band = db.get_user_band(g.user_id)
    band_id = band["id"] if band else None
    user_id = None if band_id else g.user_id
    try:
        new_default_id = db.delete_setlist(setlist_id, band_id=band_id, user_id=user_id)
    except ValueError as e:
        msg = str(e)
        return jsonify({"error": msg}), (409 if "last" in msg else 404)
    return jsonify({"ok": True, "new_default_id": new_default_id})


@app.route("/api/setlist", methods=["GET"])
@require_auth
def api_get_setlist():
    """Returns {setlist_id, settings, entries:[{song_id, plays}]}.
    Optional ?setlist_id= param to fetch a specific setlist."""
    band = db.get_user_band(g.user_id)
    sid, err = _resolve_setlist(band, g.user_id, request.args.get("setlist_id"))
    if err:
        return err
    data = db.get_setlist_full(sid)
    if not data:
        return jsonify({"error": "Set list not found"}), 404
    return jsonify(data)


@app.route("/api/setlist", methods=["POST"])
@require_auth
def api_save_setlist():
    """Body: {setlist_id?, entries:[{song_id, position, plays}]}
    Also accepts legacy bare array (saves to default setlist)."""
    body = request.get_json(force=True)
    if isinstance(body, list):
        # Legacy shape — save to default setlist.
        entries = body
        setlist_id_hint = None
    else:
        entries = (body or {}).get("entries", [])
        setlist_id_hint = (body or {}).get("setlist_id")
    if not isinstance(entries, list):
        return jsonify({"error": "entries must be an array"}), 400
    band = db.get_user_band(g.user_id)
    sid, err = _resolve_setlist(band, g.user_id, setlist_id_hint)
    if err:
        return err
    db.save_setlist_entries(sid, entries)
    return jsonify({"ok": True})


# ── Tunings API ────────────────────────────────────────────────────────────────

@app.route("/api/tunings", methods=["GET"])
@require_auth
def api_get_tunings():
    return jsonify(db.get_tunings(g.user_id))


@app.route("/api/tunings", methods=["POST"])
@require_auth
def api_add_tuning():
    data = request.get_json(force=True)
    tuning = (data or {}).get("tuning", "").strip()
    if not tuning:
        return jsonify({"error": "tuning value required"}), 400
    db.add_tuning(g.user_id, tuning)
    return jsonify({"ok": True})


# ── Initial Songs (seeding for new users) ─────────────────────────────────────

@app.route("/api/initial-songs")
@require_auth
def get_initial_songs():
    """Return hardcoded songs for initial app load; seeds the DB for new users."""
    songs = [
        {"name": "Easier to Run", "duration_raw": "3:24", "artist": "Linkin Park", "status": "For Consideration", "tuning": "Eb"},
        {"name": "The Ghost of You", "duration_raw": "3:15", "artist": "My Chemical Romance", "status": "For Consideration"},
        {"name": "White Sparrows", "duration_raw": "3:14", "artist": "Billy Talent", "status": "Learning", "tuning": "D"},
        {"name": "A Place for My Head", "duration_raw": "3:05", "artist": "Linkin Park", "status": "Learning", "tuning": "Eb"},
        {"name": "Bleed it Out", "duration_raw": "2:44", "artist": "Linkin Park", "status": "Learning"},
        {"name": "Faint", "duration_raw": "2:42", "artist": "Linkin Park", "status": "Learning"},
        {"name": "Hit the Floor", "duration_raw": "2:44", "artist": "Linkin Park", "status": "Learning"},
        {"name": "Numb", "duration_raw": "3:07", "artist": "Linkin Park", "status": "Learning", "tuning": "Eb"},
        {"name": "Hysteria", "duration_raw": "3:47", "artist": "Muse", "status": "Learning"},
        {"name": "Little Girls Pointing and Laughing", "duration_raw": "4:53", "artist": "Alexisonfire", "status": "In Rotation", "tuning": "D"},
        {"name": "Cochise", "duration_raw": "3:42", "artist": "Audioslave", "status": "In Rotation"},
        {"name": "Devil in a Midnight Mass", "duration_raw": "2:52", "artist": "Billy Talent", "status": "In Rotation"},
        {"name": "Prisoners of Today", "duration_raw": "3:53", "artist": "Billy Talent", "status": "In Rotation"},
        {"name": "River Below", "duration_raw": "3:00", "artist": "Billy Talent", "status": "In Rotation", "tuning": "D"},
        {"name": "This Is How It Goes", "duration_raw": "3:27", "artist": "Billy Talent", "status": "In Rotation"},
        {"name": "Try Honesty", "duration_raw": "4:13", "artist": "Billy Talent", "status": "In Rotation"},
        {"name": "Grey Street", "duration_raw": "5:06", "artist": "Dave Matthews Band", "status": "In Rotation"},
        {"name": "Best of You", "duration_raw": "4:15", "artist": "Foo Fighters", "status": "In Rotation"},
        {"name": "The Pretender", "duration_raw": "4:30", "artist": "Foo Fighters", "status": "In Rotation"},
        {"name": "Sex on Fire", "duration_raw": "3:23", "artist": "Kings of Leon", "status": "In Rotation"},
        {"name": "Given Up", "duration_raw": "3:09", "artist": "Linkin Park", "status": "In Rotation"},
        {"name": "School", "duration_raw": "2:42", "artist": "Nirvana", "status": "In Rotation"},
        {"name": "Bulls on Parade", "duration_raw": "3:51", "artist": "Rage Against the Machine", "status": "In Rotation"},
        {"name": "Freedom", "duration_raw": "6:09", "artist": "Rage Against the Machine", "status": "In Rotation"},
        {"name": "Killing in the Name of", "duration_raw": "5:14", "artist": "Rage Against the Machine", "status": "In Rotation"},
        {"name": "Blood Sugar Sex Magik", "duration_raw": "4:31", "artist": "Red Hot Chili Peppers", "status": "In Rotation"},
        {"name": "By the Way", "duration_raw": "3:36", "artist": "Red Hot Chili Peppers", "status": "In Rotation"},
        {"name": "Dani California", "duration_raw": "4:42", "artist": "Red Hot Chili Peppers", "status": "In Rotation"},
        {"name": "Don't Forget Me (Live at La Cigale)", "duration_raw": "6:00", "artist": "Red Hot Chili Peppers", "status": "In Rotation"},
        {"name": "Easily", "duration_raw": "3:51", "artist": "Red Hot Chili Peppers", "status": "In Rotation"},
        {"name": "My Lovely Man", "duration_raw": "4:39", "artist": "Red Hot Chili Peppers", "status": "In Rotation"},
        {"name": "Otherside", "duration_raw": "4:15", "artist": "Red Hot Chili Peppers", "status": "In Rotation"},
        {"name": "Purple Stain", "duration_raw": "4:13", "artist": "Red Hot Chili Peppers", "status": "In Rotation"},
        {"name": "Soul to Squeeze", "duration_raw": "4:49", "artist": "Red Hot Chili Peppers", "status": "In Rotation"},
        {"name": "Suck My Kiss", "duration_raw": "3:37", "artist": "Red Hot Chili Peppers", "status": "In Rotation"},
        {"name": "Can't Stop", "duration_raw": "4:29", "artist": "Red Hot Chili Peppers", "status": "Resting"},
        {"name": "Under the Bridge", "duration_raw": "4:24", "artist": "Red Hot Chili Peppers", "status": "Resting"},
    ]

    result = []
    for i, s in enumerate(songs):
        song = {
            "id": f"song-{i}",
            "name": s["name"],
            "duration_raw": s["duration_raw"],
            "duration_seconds": parse_duration(s["duration_raw"]),
            "plays": 1,
            "extra": {
                "Artist": s["artist"],
                "Status": s["status"],
                "Tuning": s.get("tuning"),
            }
        }
        saved = db.upsert_song(g.user_id, song)
        result.append(saved)

    return jsonify(result)


# ── Band API ───────────────────────────────────────────────────────────────────

@app.route("/api/band", methods=["POST"])
@require_auth
def api_create_band():
    data = request.get_json(force=True) or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Band name is required"}), 400
    if db.get_user_band(g.user_id):
        return jsonify({"error": "You are already in a band"}), 409
    band = db.create_band(g.user_id, name)
    db.migrate_songs_to_band(g.user_id, band["id"])
    return jsonify(band), 201


@app.route("/api/band", methods=["GET"])
@require_auth
def api_get_band():
    band = db.get_user_band(g.user_id)
    return jsonify(band)


@app.route("/api/band/join", methods=["POST"])
@require_auth
def api_join_band():
    data = request.get_json(force=True) or {}
    token = data.get("invite_token", "").strip()
    if not token:
        return jsonify({"error": "invite_token is required"}), 400
    try:
        band = db.join_band(g.user_id, token)
        if not band.get("already_member"):
            db.migrate_songs_to_band(g.user_id, band["id"])
        return jsonify(band)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/band/proposals", methods=["GET"])
@require_auth
def api_get_proposals():
    band = db.get_user_band(g.user_id)
    if not band:
        return jsonify([])
    return jsonify(db.get_pending_proposals(band["id"], g.user_id))


@app.route("/api/band/vote", methods=["POST"])
@require_auth
def api_cast_vote():
    data = request.get_json(force=True) or {}
    proposal_id = data.get("proposal_id", "").strip()
    vote        = data.get("vote", "").strip()
    reason      = (data.get("reason") or "").strip() or None
    if not proposal_id or not vote:
        return jsonify({"error": "proposal_id and vote are required"}), 400
    try:
        result = db.cast_vote(proposal_id, g.user_id, vote, reason=reason)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/notifications", methods=["GET"])
@require_auth
def api_get_notifications():
    return jsonify(db.get_notifications(g.user_id))


@app.route("/api/notifications/read", methods=["POST"])
@require_auth
def api_mark_notifications_read():
    data = request.get_json(force=True) or {}
    proposal_ids = data.get("proposal_ids", [])
    db.mark_notifications_read(g.user_id, proposal_ids)
    return jsonify({"ok": True})


# ── User Profile & Preferences API ───────────────────────────────────────────────

@app.route("/api/profile", methods=["GET"])
@require_auth
def api_get_profile():
    return jsonify(db.get_profile(g.user_id))


@app.route("/api/profile", methods=["PUT"])
@require_auth
def api_update_profile():
    data = request.get_json(force=True) or {}
    db.upsert_profile(g.user_id, data.get("display_name"), data.get("roles", []))
    return jsonify(db.get_profile(g.user_id))


@app.route("/api/profile/notifications", methods=["PUT"])
@require_auth
def api_update_notif_prefs():
    data = request.get_json(force=True) or {}
    db.update_notif_prefs(g.user_id, data)
    return jsonify(db.get_profile(g.user_id)["notif_prefs"])


# ── Band Management API ──────────────────────────────────────────────────────────

@app.route("/api/band", methods=["PATCH"])
@require_auth
def api_update_band():
    """Any member may rename the band or adjust the voting threshold."""
    band = db.get_user_band(g.user_id)
    if not band:
        return jsonify({"error": "You are not in a band"}), 404
    data = request.get_json(force=True) or {}
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Band name is required"}), 400
        db.rename_band(band["id"], name)
    if "approval_factor" in data:
        try:
            db.set_approval_factor(band["id"], float(data["approval_factor"]))
        except (TypeError, ValueError):
            return jsonify({"error": "approval_factor must be a number"}), 400
    if "time_defaults" in data:
        try:
            db.set_band_time_defaults(band["id"], data["time_defaults"])
        except (TypeError, ValueError) as e:
            return jsonify({"error": str(e) or "invalid time_defaults"}), 400
    return jsonify(db.get_user_band(g.user_id))


@app.route("/api/band/invite/regenerate", methods=["POST"])
@require_auth
def api_regenerate_invite():
    band = db.get_user_band(g.user_id)
    if not band:
        return jsonify({"error": "You are not in a band"}), 404
    token = db.regenerate_invite(band["id"])
    return jsonify({"invite_token": token})


@app.route("/api/band/leave", methods=["POST"])
@require_auth
def api_leave_band():
    band = db.get_user_band(g.user_id)
    if not band:
        return jsonify({"error": "You are not in a band"}), 404
    try:
        db.leave_band(g.user_id, band["id"])
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 409


@app.route("/api/band/members/<user_id>", methods=["PATCH"])
@require_auth
def api_promote_member(user_id):
    """Admin-only: promote a member to admin."""
    band = db.get_user_band(g.user_id)
    if not band:
        return jsonify({"error": "You are not in a band"}), 404
    if band.get("role") != "admin":
        return jsonify({"error": "Only an admin can change roles"}), 403
    db.promote_member(band["id"], user_id)
    return jsonify(db.get_user_band(g.user_id))


@app.route("/api/band/members/<user_id>", methods=["DELETE"])
@require_auth
def api_remove_member(user_id):
    """Admin-only: remove a member from the band."""
    band = db.get_user_band(g.user_id)
    if not band:
        return jsonify({"error": "You are not in a band"}), 404
    if band.get("role") != "admin":
        return jsonify({"error": "Only an admin can remove members"}), 403
    try:
        db.remove_member(band["id"], user_id)
        return jsonify(db.get_user_band(g.user_id))
    except ValueError as e:
        return jsonify({"error": str(e)}), 409


@app.route("/api/band", methods=["DELETE"])
@require_auth
def api_delete_band():
    """Admin-only: delete the entire band."""
    band = db.get_user_band(g.user_id)
    if not band:
        return jsonify({"error": "You are not in a band"}), 404
    if band.get("role") != "admin":
        return jsonify({"error": "Only an admin can delete the band"}), 403
    db.delete_band(band["id"])
    return jsonify({"ok": True})


# ── Spotify / YouTube / Album Art (stateless proxy routes) ────────────────────

def get_spotify_token():
    """Get Spotify access token using Client Credentials flow."""
    if _config["spotify_access_token"] and time.time() < _config["spotify_token_expiry"]:
        return _config["spotify_access_token"]

    client_id = _config.get("spotify_client_id", "")
    client_secret = _config.get("spotify_client_secret", "")

    if not client_id or not client_secret:
        return None

    try:
        auth_str = f"{client_id}:{client_secret}"
        auth_b64 = base64.b64encode(auth_str.encode()).decode()

        url = "https://accounts.spotify.com/api/token"
        req = urllib.request.Request(url, method="POST")
        req.add_header("Authorization", f"Basic {auth_b64}")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

        data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()

        with urllib.request.urlopen(req, data, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            token = result.get("access_token")
            expires_in = result.get("expires_in", 3600)
            _config["spotify_access_token"] = token
            _config["spotify_token_expiry"] = time.time() + expires_in - 60
            return token
    except Exception as e:
        print(f"Spotify auth error: {e}")
        return None


@app.route("/api/spotify-search", methods=["POST"])
def spotify_search():
    """Search Spotify for a track."""
    data = request.get_json(force=True)
    query = data.get("query", "").strip()

    if not query:
        return jsonify({"error": "Query missing"}), 400

    token = get_spotify_token()
    if not token:
        return jsonify({"error": "Spotify authentication failed"}), 500

    try:
        url = f"https://api.spotify.com/v1/search?q={urllib.parse.quote(query)}&type=track&limit=10"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {token}")

        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())

        tracks = []
        for item in result.get("tracks", {}).get("items", []):
            duration_ms = item.get("duration_ms", 0)
            duration_sec = duration_ms // 1000
            minutes = duration_sec // 60
            seconds = duration_sec % 60

            image_url = ""
            if item.get("album", {}).get("images"):
                image_url = item["album"]["images"][0]["url"]

            artist_name = item.get("artists", [{}])[0].get("name", "Unknown")

            tracks.append({
                "id": item.get("id", ""),
                "name": item.get("name", ""),
                "artist": artist_name,
                "duration_sec": duration_sec,
                "duration_str": f"{minutes}:{seconds:02d}",
                "image": image_url,
                "preview_url": item.get("preview_url", ""),
                "spotify_url": item.get("external_urls", {}).get("spotify", ""),
            })

        return jsonify({"results": tracks})
    except Exception as e:
        return jsonify({"error": f"Spotify search failed: {e}"}), 500


@app.route("/api/youtube-search", methods=["POST"])
def youtube_search():
    """Search YouTube for a song."""
    data = request.get_json(force=True)
    query = data.get("query", "").strip()
    api_key = _config.get("youtube_api_key", "")

    if not query or not api_key:
        return jsonify({"error": "Query or API key missing"}), 400

    try:
        url = f"https://www.googleapis.com/youtube/v3/search?part=snippet&q={urllib.parse.quote(query)}&type=video&maxResults=10&key={api_key}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        results = []
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            results.append({
                "videoId": item.get("id", {}).get("videoId", ""),
                "title": snippet.get("title", ""),
                "channelTitle": snippet.get("channelTitle", ""),
                "thumbnail": snippet.get("thumbnails", {}).get("medium", {}).get("url", ""),
                "url": f"https://www.youtube.com/watch?v={item.get('id', {}).get('videoId', '')}",
            })
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": f"YouTube search failed: {e}"}), 500


@app.route("/api/album-art/<artist>/<track>")
def get_album_art(artist, track):
    """Fetch album art from iTunes API (free, no auth needed)."""
    try:
        query = f"{track} {artist}"
        url = f"https://itunes.apple.com/search?term={urllib.parse.quote(query)}&entity=song&limit=1"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if data.get("results"):
            artwork_url = data["results"][0].get("artworkUrl100", "")
            artwork_url = artwork_url.replace("100x100", "200x200")
            return jsonify({"artworkUrl": artwork_url})
    except Exception:
        pass

    return jsonify({"artworkUrl": ""})


if __name__ == "__main__":
    # Port is configurable so a throwaway preview server can run alongside the
    # persistent launchd server (which owns 5050). CLI arg wins, then $PORT,
    # else the default 5050. See CLAUDE.md "Keeping localhost up".
    _port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 5050))
    print(f"Rehearsal Planner running at http://localhost:{_port}")
    # When supervised by launchd (RP_SERVICE=1) run a single process so the
    # supervisor cleanly owns/restarts it; the Werkzeug auto-reloader's extra
    # child process confuses external supervisors. Plain `python3 app.py` keeps
    # the reloader on for normal local dev. Templates still hot-reload either way.
    _service = os.environ.get("RP_SERVICE") == "1"
    app.run(debug=True, port=_port, use_reloader=not _service)
