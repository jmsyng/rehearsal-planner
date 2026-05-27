from dotenv import load_dotenv
load_dotenv()

import json
import os
import urllib.request
import urllib.parse
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


# ── Songs API ──────────────────────────────────────────────────────────────────

@app.route("/api/songs", methods=["GET"])
@require_auth
def api_get_songs():
    return jsonify(db.get_songs(g.user_id))


@app.route("/api/songs", methods=["POST"])
@require_auth
def api_add_song():
    song = request.get_json(force=True)
    if not song or not song.get("id") or not song.get("name"):
        return jsonify({"error": "id and name are required"}), 400
    saved = db.upsert_song(g.user_id, song)
    return jsonify(saved), 201


@app.route("/api/songs/<song_id>", methods=["PUT"])
@require_auth
def api_update_song(song_id):
    song = request.get_json(force=True)
    song["id"] = song_id
    saved = db.upsert_song(g.user_id, song)
    return jsonify(saved)


@app.route("/api/songs/<song_id>", methods=["DELETE"])
@require_auth
def api_delete_song(song_id):
    deleted = db.delete_song(g.user_id, song_id)
    if not deleted:
        return jsonify({"error": "Song not found"}), 404
    return jsonify({"ok": True})


# ── Set List API ───────────────────────────────────────────────────────────────

@app.route("/api/setlist", methods=["GET"])
@require_auth
def api_get_setlist():
    return jsonify(db.get_setlist(g.user_id))


@app.route("/api/setlist", methods=["POST"])
@require_auth
def api_save_setlist():
    """Body: [{"song_id": str, "position": int, "plays": int}, ...]"""
    entries = request.get_json(force=True)
    if not isinstance(entries, list):
        return jsonify({"error": "Expected array"}), 400
    db.save_setlist(g.user_id, entries)
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
        db.upsert_song(g.user_id, song)
        result.append(song)

    return jsonify(result)


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
    print("Rehearsal Planner running at http://localhost:5050")
    app.run(debug=True, port=5050)
