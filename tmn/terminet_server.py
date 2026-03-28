#!/usr/bin/env python3
"""
terminet_server.py  —  Terminet LAN Server  v4
  pip install flask bcrypt
  python terminet_server.py [--port 5151] [--host 0.0.0.0]

  New in v4:
    • SQLite backend  (data.db, auto-migrates from old data.txt)
    • Likes  (/like/<post_id>)
    • Search  (/search?q=&type=posts|users|all)
    • Admin commands  (/admin/*)  — first registered user becomes admin
    • Username profile lookup  (/profile/by/<username>)
    • Live notification poll  (/notifications/poll)  — long-poll ~20 s
"""

import json, os, hashlib, secrets, argparse, socket, sys, re, threading, time
from datetime import datetime, timedelta
from collections import defaultdict
from flask import Flask, request, jsonify, abort, make_response

try:
    import bcrypt as _bcrypt
    BCRYPT_OK = True
except ImportError:
    BCRYPT_OK = False

try:
    import sqlite3
    SQLITE_OK = True
except ImportError:
    SQLITE_OK = False

# ── ANSI ───────────────────────────────────────────────────────────────────────
def _enable_ansi():
    if not sys.stdout.isatty(): return False
    if sys.platform != "win32": return True
    try:
        import ctypes, ctypes.wintypes
        k = ctypes.windll.kernel32; h = k.GetStdHandle(-11)
        m = ctypes.wintypes.DWORD()
        if not k.GetConsoleMode(h, ctypes.byref(m)): return False
        VT = 0x0004
        if m.value & VT: return True
        return bool(k.SetConsoleMode(h, m.value | VT))
    except: return False

_A = _enable_ansi()
def _c(x): return x if _A else ""
_R=_c("\033[0m"); _B=_c("\033[1m"); _CY=_c("\033[96m")
_GR=_c("\033[92m"); _YL=_c("\033[93m"); _GY=_c("\033[90m")
_RD=_c("\033[91m"); _MG=_c("\033[95m")

def log(tag, msg, color=_GY):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"  {color}{ts}  {tag:<8}{_R}  {msg}", flush=True)

# ── paths ──────────────────────────────────────────────────────────────────────
BASE       = os.path.dirname(os.path.abspath(__file__))
DB_FILE    = os.path.join(BASE, "data.db")
OLD_DATA   = os.path.join(BASE, "data.txt")
_db_lock   = threading.RLock()

app = Flask(__name__)

@app.errorhandler(Exception)
def handle_exception(e):
    import traceback; traceback.print_exc()
    return jsonify({"error": f"Server error: {e}"}), 500

@app.errorhandler(404)
def handle_404(e): return jsonify({"error": "Endpoint not found"}), 404

# ── rate limiting ──────────────────────────────────────────────────────────────
_rl_login = defaultdict(list)
_rl_post  = defaultdict(list)

def _rate_limit(store, key, max_hits, window_sec):
    now = datetime.utcnow(); cutoff = now - timedelta(seconds=window_sec)
    store[key] = [t for t in store[key] if t > cutoff]
    if len(store[key]) >= max_hits: return True
    store[key].append(now); return False

def client_ip():
    """Real client IP — works behind Cloudflare Tunnel and other proxies."""
    return (request.headers.get("CF-Connecting-IP")
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr)

# ── SQLite DB ──────────────────────────────────────────────────────────────────
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    username    TEXT UNIQUE NOT NULL,
    password    TEXT NOT NULL,
    joined      TEXT NOT NULL,
    is_admin    INTEGER DEFAULT 0,
    is_banned   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS follows (
    follower_id TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    PRIMARY KEY (follower_id, target_id)
);

CREATE TABLE IF NOT EXISTS posts (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    username    TEXT NOT NULL,
    text        TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    parent_id   TEXT REFERENCES posts(id)
);

CREATE TABLE IF NOT EXISTS likes (
    post_id     TEXT NOT NULL REFERENCES posts(id),
    user_id     TEXT NOT NULL,
    PRIMARY KEY (post_id, user_id)
);

CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id   TEXT NOT NULL,
    type        TEXT NOT NULL,
    from_user   TEXT NOT NULL,
    from_id     TEXT NOT NULL,
    post_ref    TEXT NOT NULL,
    text        TEXT NOT NULL,
    ts          TEXT NOT NULL,
    read        INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    from_id     TEXT NOT NULL,
    from_user   TEXT NOT NULL,
    to_id       TEXT NOT NULL,
    to_user     TEXT NOT NULL,
    text        TEXT NOT NULL,
    timestamp   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS counters (
    kind TEXT PRIMARY KEY,
    val  INTEGER DEFAULT 0
);

INSERT OR IGNORE INTO counters VALUES ('user',0),('post',0),('msg',0);
"""

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS channels (
    id          TEXT PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    description TEXT DEFAULT '',
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_members (
    channel_id  TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    PRIMARY KEY (channel_id, user_id)
);
CREATE TABLE IF NOT EXISTS polls (
    post_id     TEXT PRIMARY KEY REFERENCES posts(id),
    question    TEXT NOT NULL,
    options     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS poll_votes (
    post_id     TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    option_idx  INTEGER NOT NULL,
    PRIMARY KEY (post_id, user_id)
);
CREATE TABLE IF NOT EXISTS announcements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id    TEXT NOT NULL,
    admin_user  TEXT NOT NULL,
    text        TEXT NOT NULL,
    ts          TEXT NOT NULL
);
INSERT OR IGNORE INTO counters VALUES ('channel',0);
"""

def upgrade_schema():
    """Non-destructive column additions for existing databases."""
    conn = db()
    for stmt in [
        "ALTER TABLE users ADD COLUMN bio TEXT DEFAULT ''",
        "ALTER TABLE posts ADD COLUMN edited INTEGER DEFAULT 0",
        "ALTER TABLE posts ADD COLUMN channel_id TEXT",
    ]:
        try: conn.execute(stmt); conn.commit()
        except: pass  # column already exists

def get_db():
    """Return a thread-local sqlite3 connection."""
    local = threading.local()
    if not hasattr(local, "conn") or local.conn is None:
        local.conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        local.conn.row_factory = sqlite3.Row
    return local.conn

# Use a single shared connection protected by a lock for simplicity
_conn = None

def db():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _conn.executescript(SCHEMA_V2)
        _conn.commit()
    return _conn

def next_id(kind):
    prefix = {"user":"U","post":"P","msg":"M","channel":"C"}[kind]
    with _db_lock:
        cur = db().execute("UPDATE counters SET val=val+1 WHERE kind=? RETURNING val", (kind,))
        val = cur.fetchone()[0]
        db().commit()
    return f"{prefix}{val:04d}"

# ── migration from data.txt ────────────────────────────────────────────────────
def maybe_migrate():
    if not os.path.exists(OLD_DATA): return
    if db().execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
        log("MIGRATE", "SQLite DB already has data — skipping data.txt import.", _YL)
        return
    log("MIGRATE", f"Importing {OLD_DATA} into SQLite …", _CY)
    try:
        with open(OLD_DATA, "r", encoding="utf-8") as f:
            old = json.load(f)
        conn = db()
        # counters
        c = old.get("counters", {})
        for k, v in c.items():
            if k in ("user","post","msg"):
                conn.execute("UPDATE counters SET val=? WHERE kind=?", (v, k))
        # tokens map: username → token (inverted for auth)
        tok_map = {v: k for k, v in old.get("tokens",{}).items()}  # username→token
        # users
        for uname, u in old.get("users",{}).items():
            conn.execute("""INSERT OR IGNORE INTO users
                (id,username,password,joined,is_admin,is_banned)
                VALUES (?,?,?,?,0,0)""",
                (u["id"], uname, u["password"],
                 u.get("joined", datetime.now().isoformat())))
            for tid in u.get("following", []):
                conn.execute("INSERT OR IGNORE INTO follows VALUES (?,?)", (u["id"], tid))
        # posts
        for pid, p in old.get("posts",{}).items():
            conn.execute("""INSERT OR IGNORE INTO posts
                (id,user_id,username,text,timestamp,parent_id)
                VALUES (?,?,?,?,?,?)""",
                (pid, p["user_id"], p["username"], p["text"],
                 p.get("timestamp", datetime.now().isoformat()),
                 p.get("parent")))
            for liker in p.get("likes", []):
                conn.execute("INSERT OR IGNORE INTO likes VALUES (?,?)", (pid, liker))
        # notifications
        for uid, notifs in old.get("notifications",{}).items():
            for n in notifs:
                conn.execute("""INSERT INTO notifications
                    (target_id,type,from_user,from_id,post_ref,text,ts,read)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (uid, n["type"], n.get("from","?"), n.get("from_id","?"),
                     n.get("post","?"), n.get("text",""), n.get("ts",""),
                     1 if n.get("read") else 0))
        # messages
        for key, msgs in old.get("messages",{}).items():
            for m in msgs:
                conn.execute("""INSERT OR IGNORE INTO messages
                    (id,from_id,from_user,to_id,to_user,text,timestamp)
                    VALUES (?,?,?,?,?,?,?)""",
                    (m["id"], m["from_id"], m["from"],
                     m["to_id"], m["to"], m["text"], m["timestamp"]))
        conn.commit()
        log("MIGRATE", "Migration complete.", _GR)
        os.rename(OLD_DATA, OLD_DATA + ".migrated")
    except Exception as e:
        log("MIGRATE", f"Failed: {e}", _RD)

# ── passwords ──────────────────────────────────────────────────────────────────
def hash_pw(pw):
    if BCRYPT_OK: return _bcrypt.hashpw(pw.encode(), _bcrypt.gensalt()).decode()
    salt = secrets.token_hex(16)
    return "sha256:" + salt + ":" + hashlib.sha256((salt+pw).encode()).hexdigest()

def check_pw(pw, stored):
    if stored.startswith("$2"):
        return BCRYPT_OK and _bcrypt.checkpw(pw.encode(), stored.encode())
    if stored.startswith("sha256:"):
        _, salt, h = stored.split(":", 2)
        return hashlib.sha256((salt+pw).encode()).hexdigest() == h
    return hashlib.sha256(pw.encode()).hexdigest() == stored

# ── tokens (in-memory, fast) ───────────────────────────────────────────────────
_tokens = {}   # token → username

def _load_tokens():
    """Tokens aren't persisted across restarts — users must re-login. That's fine."""
    pass

def issue_token(username):
    t = secrets.token_hex(32); _tokens[t] = username; return t

def auth_user(required=True):
    token = request.headers.get("Authorization","").removeprefix("Bearer ").strip()
    username = _tokens.get(token) if token else None
    if not username:
        if required:
            abort(make_response(jsonify({"error":"Not logged in or session expired."}), 401))
        return None
    row = db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row:
        if required: abort(make_response(jsonify({"error":"User not found."}), 401))
        return None
    if row["is_banned"]:
        abort(make_response(jsonify({"error":"Your account has been banned."}), 403))
    return row

# ── helpers ────────────────────────────────────────────────────────────────────
def post_to_dict(row, viewer_id=None):
    if row is None: return None
    d = dict(row)
    pid = d["id"]
    d["likes"]   = [r[0] for r in db().execute(
        "SELECT user_id FROM likes WHERE post_id=?", (pid,)).fetchall()]
    d["like_count"] = len(d["likes"])
    d["liked_by_me"] = viewer_id in d["likes"] if viewer_id else False
    d["replies"] = [r[0] for r in db().execute(
        "SELECT id FROM posts WHERE parent_id=? ORDER BY timestamp", (pid,)).fetchall()]
    d["parent"]  = d.pop("parent_id", None)
    # poll data
    poll_row = db().execute("SELECT * FROM polls WHERE post_id=?", (pid,)).fetchone()
    if poll_row:
        options = json.loads(poll_row["options"])
        vote_rows = db().execute(
            "SELECT option_idx, COUNT(*) as cnt FROM poll_votes WHERE post_id=? GROUP BY option_idx",
            (pid,)).fetchall()
        vote_counts = {r["option_idx"]: r["cnt"] for r in vote_rows}
        my_vote = None
        if viewer_id:
            v = db().execute("SELECT option_idx FROM poll_votes WHERE post_id=? AND user_id=?",
                (pid, viewer_id)).fetchone()
            if v: my_vote = v["option_idx"]
        d["poll"] = {
            "question": poll_row["question"],
            "options":  options,
            "votes":    [vote_counts.get(i, 0) for i in range(len(options))],
            "my_vote":  my_vote,
            "total":    sum(vote_counts.values())
        }
    return d

def push_notif(target_id, kind, actor, post_ref, text):
    db().execute("""INSERT INTO notifications
        (target_id,type,from_user,from_id,post_ref,text,ts,read)
        VALUES (?,?,?,?,?,?,?,0)""",
        (target_id, kind, actor["username"], actor["id"],
         post_ref, text[:80], datetime.now().isoformat()))
    db().commit()
    # wake up any long-pollers
    _notify_event.set()
    _notify_event.clear()

_notify_event = threading.Event()

def push_notifs_for_text(actor, text, post_id, kind, parent_author_id=None):
    done = set()
    for name in re.findall(r"@(\w+)", text):
        row = db().execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
        if row and row["id"] != actor["id"] and row["id"] not in done:
            push_notif(row["id"], "mention", actor, post_id, text)
            done.add(row["id"])
    if kind == "reply" and parent_author_id and parent_author_id != actor["id"] \
            and parent_author_id not in done:
        push_notif(parent_author_id, "reply", actor, post_id, text)

def require_admin(user):
    if not user["is_admin"]:
        abort(make_response(jsonify({"error":"Admin privileges required."}), 403))

# ══════════════════════════════════════════════════════════════════════════════
#  Routes
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/ping")
def ping():
    users = db().execute("SELECT COUNT(*) FROM users").fetchone()[0]
    posts = db().execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    return jsonify({"status":"ok","server":"Terminet LAN","users":users,
                    "posts":posts,"version":"4"})

# ── auth ───────────────────────────────────────────────────────────────────────
@app.route("/register", methods=["POST"])
def register():
    body = request.get_json(force=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not re.match(r"^\w{3,24}$", username):
        return jsonify({"error":"Username must be 3-24 chars (letters/numbers/underscore)."}), 400
    if len(password) < 4:
        return jsonify({"error":"Password must be at least 4 characters."}), 400
    with _db_lock:
        if db().execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            return jsonify({"error":f"Username '{username}' is already taken."}), 409
        uid = next_id("user")
        # first user becomes admin
        is_admin = 1 if db().execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0 else 0
        db().execute("""INSERT INTO users (id,username,password,joined,is_admin,is_banned)
            VALUES (?,?,?,?,?,0)""",
            (uid, username, hash_pw(password), datetime.now().isoformat(), is_admin))
        db().commit()
    token = issue_token(username)
    log("REGISTER", f"@{username} ({uid})" + (" [ADMIN]" if is_admin else ""), _GR)
    return jsonify({"user_id":uid,"username":username,"token":token,"is_admin":bool(is_admin)}), 201

@app.route("/login", methods=["POST"])
def login():
    ip = client_ip()
    if _rate_limit(_rl_login, ip, 10, 60):
        return jsonify({"error":"Too many login attempts. Wait a minute."}), 429
    body = request.get_json(force=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    row = db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row or not check_pw(password, row["password"]):
        return jsonify({"error":"Invalid username or password."}), 401
    if row["is_banned"]:
        return jsonify({"error":"Your account has been banned."}), 403
    token = issue_token(username)
    log("LOGIN", f"@{username}", _GR)
    return jsonify({"user_id":row["id"],"username":username,"token":token,
                    "is_admin":bool(row["is_admin"])})

@app.route("/logout", methods=["POST"])
def logout():
    token = request.headers.get("Authorization","").removeprefix("Bearer ").strip()
    _tokens.pop(token, None)
    return jsonify({"status":"ok"})

# ── posts ───────────────────────────────────────────────────────────────────────
@app.route("/post", methods=["POST"])
def create_post():
    with _db_lock:
        user = auth_user()
        if _rate_limit(_rl_post, user["id"], 20, 60):
            return jsonify({"error":"Slow down! Max 20 posts per minute."}), 429
        text = (request.get_json(force=True).get("text") or "").strip()
        if not text: return jsonify({"error":"Post text cannot be empty."}), 400
        if len(text) > 500: return jsonify({"error":"Post too long (max 500 chars)."}), 400
        pid = next_id("post")
        db().execute("INSERT INTO posts (id,user_id,username,text,timestamp,parent_id) VALUES (?,?,?,?,?,NULL)",
            (pid, user["id"], user["username"], text, datetime.now().isoformat()))
        db().commit()
        push_notifs_for_text(user, text, pid, "post")
    log("POST", f"@{user['username']} {pid}  {text[:40]}", _YL)
    return jsonify({"post_id":pid,"post":post_to_dict(
        db().execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone(), user["id"])}), 201

@app.route("/reply/<post_id>", methods=["POST"])
def create_reply(post_id):
    with _db_lock:
        user  = auth_user()
        parent = db().execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        if not parent: return jsonify({"error":f"Post '{post_id}' not found."}), 404
        text = (request.get_json(force=True).get("text") or "").strip()
        if not text: return jsonify({"error":"Reply text cannot be empty."}), 400
        if len(text) > 500: return jsonify({"error":"Reply too long (max 500 chars)."}), 400
        rid = next_id("post")
        db().execute("INSERT INTO posts (id,user_id,username,text,timestamp,parent_id) VALUES (?,?,?,?,?,?)",
            (rid, user["id"], user["username"], text, datetime.now().isoformat(), post_id))
        db().commit()
        push_notifs_for_text(user, text, rid, "reply", parent["user_id"])
    log("REPLY", f"@{user['username']} {rid} on {post_id}", _YL)
    return jsonify({"reply_id":rid,"post":post_to_dict(
        db().execute("SELECT * FROM posts WHERE id=?", (rid,)).fetchone(), user["id"])}), 201

# ── likes ────────────────────────────────────────────────────────────────────
@app.route("/like/<post_id>", methods=["POST"])
def toggle_like(post_id):
    with _db_lock:
        user = auth_user()
        post = db().execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        if not post: return jsonify({"error":f"Post '{post_id}' not found."}), 404
        existing = db().execute("SELECT 1 FROM likes WHERE post_id=? AND user_id=?",
                                (post_id, user["id"])).fetchone()
        if existing:
            db().execute("DELETE FROM likes WHERE post_id=? AND user_id=?", (post_id, user["id"]))
            action = "unliked"
        else:
            db().execute("INSERT INTO likes VALUES (?,?)", (post_id, user["id"]))
            action = "liked"
            if post["user_id"] != user["id"]:
                push_notif(post["user_id"], "like", user, post_id, post["text"])
        db().commit()
    count = db().execute("SELECT COUNT(*) FROM likes WHERE post_id=?", (post_id,)).fetchone()[0]
    log("LIKE", f"@{user['username']} {action} {post_id}", _MG)
    return jsonify({"status":action,"post_id":post_id,"like_count":count})

# ── read ────────────────────────────────────────────────────────────────────────
def _build_feed_response(rows, page, size, viewer_id):
    posts = [post_to_dict(r, viewer_id) for r in rows]
    has_more = len(posts) > size
    return jsonify({"posts":posts[:size], "page":page, "has_more":has_more, "total":len(posts)})

@app.route("/feed")
def feed():
    user = auth_user(required=False)
    vid  = user["id"] if user else None
    page = max(1, int(request.args.get("page",1))); size = 20
    rows = db().execute(
        "SELECT * FROM posts WHERE parent_id IS NULL ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        (size+1, (page-1)*size)).fetchall()
    return _build_feed_response(rows, page, size, vid)

@app.route("/feed/following")
def feed_following():
    user = auth_user()
    page = max(1, int(request.args.get("page",1))); size = 20
    rows = db().execute("""
        SELECT p.* FROM posts p
        JOIN follows f ON f.target_id = p.user_id
        WHERE f.follower_id=? AND p.parent_id IS NULL
        ORDER BY p.timestamp DESC LIMIT ? OFFSET ?""",
        (user["id"], size+1, (page-1)*size)).fetchall()
    return _build_feed_response(rows, page, size, user["id"])

@app.route("/profile/<user_id>")
def profile(user_id):
    viewer = auth_user(required=False)
    vid = viewer["id"] if viewer else None
    row = db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not row: return jsonify({"error":f"No user with ID '{user_id}'."}), 404
    return _profile_response(row, vid)

@app.route("/profile/by/<username>")
def profile_by_username(username):
    viewer = auth_user(required=False)
    vid = viewer["id"] if viewer else None
    row = db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row: return jsonify({"error":f"No user '@{username}'."}), 404
    return _profile_response(row, vid)

def _profile_response(row, viewer_id):
    safe = {k: row[k] for k in row.keys() if k not in ("password",)}
    safe["following"] = [r[0] for r in db().execute(
        "SELECT target_id FROM follows WHERE follower_id=?", (row["id"],)).fetchall()]
    safe["followers_count"] = db().execute(
        "SELECT COUNT(*) FROM follows WHERE target_id=?", (row["id"],)).fetchone()[0]
    safe["is_followed_by_me"] = viewer_id in [r[0] for r in db().execute(
        "SELECT follower_id FROM follows WHERE target_id=?", (row["id"],)).fetchall()] \
        if viewer_id else False
    posts = db().execute(
        "SELECT * FROM posts WHERE user_id=? ORDER BY timestamp DESC", (row["id"],)).fetchall()
    return jsonify({"user":safe,
                    "posts":[post_to_dict(p, viewer_id) for p in posts]})

# ── search ─────────────────────────────────────────────────────────────────────
@app.route("/search")
def search():
    q    = (request.args.get("q") or "").strip()
    kind = request.args.get("type", "all").lower()   # posts | users | all
    if not q or len(q) < 2:
        return jsonify({"error":"Query must be at least 2 characters."}), 400
    viewer = auth_user(required=False)
    vid = viewer["id"] if viewer else None
    result = {}
    like = f"%{q}%"
    if kind in ("posts","all"):
        rows = db().execute(
            "SELECT * FROM posts WHERE text LIKE ? ORDER BY timestamp DESC LIMIT 30",
            (like,)).fetchall()
        result["posts"] = [post_to_dict(r, vid) for r in rows]
    if kind in ("users","all"):
        rows = db().execute(
            "SELECT * FROM users WHERE username LIKE ? AND is_banned=0 LIMIT 20",
            (like,)).fetchall()
        result["users"] = [
            {"id":r["id"],"username":r["username"],"joined":r["joined"],
             "is_admin":bool(r["is_admin"])} for r in rows]
    result["query"] = q
    return jsonify(result)

# ── bio ─────────────────────────────────────────────────────────────────────────
@app.route("/profile/bio", methods=["POST"])
def set_bio():
    user = auth_user()
    bio  = (request.get_json(force=True).get("bio") or "").strip()
    if len(bio) > 160: return jsonify({"error":"Bio max 160 chars."}), 400
    with _db_lock:
        db().execute("UPDATE users SET bio=? WHERE id=?", (bio, user["id"]))
        db().commit()
    log("BIO", f"@{user['username']} updated bio", _GY)
    return jsonify({"status":"ok","bio":bio})

# ── edit / delete own posts ─────────────────────────────────────────────────────
@app.route("/post/<post_id>", methods=["PATCH"])
def edit_post(post_id):
    user = auth_user()
    post = db().execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if not post: return jsonify({"error":"Post not found."}), 404
    if post["user_id"] != user["id"]: return jsonify({"error":"Not your post."}), 403
    text = (request.get_json(force=True).get("text") or "").strip()
    if not text: return jsonify({"error":"Text cannot be empty."}), 400
    if len(text) > 500: return jsonify({"error":"Post too long (max 500 chars)."}), 400
    with _db_lock:
        db().execute("UPDATE posts SET text=?, edited=1 WHERE id=?", (text, post_id))
        db().commit()
    log("EDIT", f"@{user['username']} edited {post_id}", _YL)
    return jsonify({"status":"edited","post":post_to_dict(
        db().execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone(), user["id"])})

@app.route("/post/<post_id>", methods=["DELETE"])
def delete_own_post(post_id):
    user = auth_user()
    post = db().execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if not post: return jsonify({"error":"Post not found."}), 404
    if post["user_id"] != user["id"] and not user["is_admin"]:
        return jsonify({"error":"Not your post."}), 403
    with _db_lock:
        db().execute("DELETE FROM poll_votes WHERE post_id=?", (post_id,))
        db().execute("DELETE FROM polls WHERE post_id=?", (post_id,))
        db().execute("DELETE FROM likes WHERE post_id=?", (post_id,))
        db().execute("DELETE FROM posts WHERE id=?", (post_id,))
        db().commit()
    log("DELETE", f"@{user['username']} deleted {post_id}", _RD)
    return jsonify({"status":"deleted","post_id":post_id})

# ── view single post + thread ──────────────────────────────────────────────────
@app.route("/post/<post_id>", methods=["GET"])
def get_post(post_id):
    viewer = auth_user(required=False)
    vid = viewer["id"] if viewer else None
    post = db().execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if not post:
        return jsonify({"error": f"Post '{post_id}' not found."}), 404

    def collect_thread(pid, depth=0):
        """Recursively collect a post and its replies, depth-first."""
        row = db().execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
        if not row:
            return []
        d = post_to_dict(row, vid)
        d["depth"] = depth
        result = [d]
        for reply_id in d.get("replies", []):
            result.extend(collect_thread(reply_id, depth + 1))
        return result

    thread = collect_thread(post_id)
    return jsonify({"thread": thread})

# ── polls ────────────────────────────────────────────────────────────────────────
@app.route("/poll", methods=["POST"])
def create_poll():
    with _db_lock:
        user = auth_user()
        body = request.get_json(force=True) or {}
        question   = (body.get("question") or "").strip()
        options    = body.get("options", [])
        channel_id = body.get("channel_id")
        if not question: return jsonify({"error":"Question required."}), 400
        if len(options) < 2 or len(options) > 8:
            return jsonify({"error":"Need 2-8 options."}), 400
        options = [str(o).strip()[:80] for o in options if str(o).strip()]
        pid = next_id("post")
        db().execute(
            "INSERT INTO posts (id,user_id,username,text,timestamp,parent_id,channel_id) VALUES (?,?,?,?,?,NULL,?)",
            (pid, user["id"], user["username"], f"📊 {question}",
             datetime.now().isoformat(), channel_id))
        db().execute("INSERT INTO polls (post_id,question,options) VALUES (?,?,?)",
            (pid, question, json.dumps(options)))
        db().commit()
    log("POLL", f"@{user['username']} created poll {pid}", _YL)
    return jsonify({"post_id":pid,"post":post_to_dict(
        db().execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone(), user["id"])}), 201

@app.route("/poll/<post_id>/vote", methods=["POST"])
def vote_poll(post_id):
    with _db_lock:
        user = auth_user()
        poll = db().execute("SELECT * FROM polls WHERE post_id=?", (post_id,)).fetchone()
        if not poll: return jsonify({"error":"Poll not found."}), 404
        options = json.loads(poll["options"])
        idx = request.get_json(force=True).get("option")
        if idx is None or not (0 <= int(idx) < len(options)):
            return jsonify({"error":f"Option must be 0-{len(options)-1}."}), 400
        idx = int(idx)
        if db().execute("SELECT 1 FROM poll_votes WHERE post_id=? AND user_id=?",
                (post_id, user["id"])).fetchone():
            db().execute("UPDATE poll_votes SET option_idx=? WHERE post_id=? AND user_id=?",
                (idx, post_id, user["id"]))
        else:
            db().execute("INSERT INTO poll_votes VALUES (?,?,?)", (post_id, user["id"], idx))
        db().commit()
    return jsonify({"status":"voted","option":idx,"post":post_to_dict(
        db().execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone(), user["id"])})

# ── channels ─────────────────────────────────────────────────────────────────────
@app.route("/channels", methods=["GET"])
def list_channels():
    viewer = auth_user(required=False)
    vid    = viewer["id"] if viewer else None
    rows   = db().execute("SELECT * FROM channels ORDER BY name").fetchall()
    result = []
    for r in rows:
        c = dict(r)
        c["member_count"] = db().execute(
            "SELECT COUNT(*) FROM channel_members WHERE channel_id=?", (r["id"],)).fetchone()[0]
        c["is_member"] = bool(db().execute(
            "SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",
            (r["id"], vid)).fetchone()) if vid else False
        result.append(c)
    return jsonify({"channels":result})

@app.route("/channel/create", methods=["POST"])
def create_channel():
    with _db_lock:
        user = auth_user()
        body = request.get_json(force=True) or {}
        name = re.sub(r"[^\w-]", "", (body.get("name") or "").strip().lower())
        desc = (body.get("description") or "").strip()[:160]
        if not name or len(name) < 2:
            return jsonify({"error":"Channel name: 2+ chars, letters/numbers/- only."}), 400
        if len(name) > 32:
            return jsonify({"error":"Channel name max 32 chars."}), 400
        if db().execute("SELECT 1 FROM channels WHERE name=?", (name,)).fetchone():
            return jsonify({"error":f"Channel #{name} already exists."}), 409
        cid = next_id("channel")
        db().execute(
            "INSERT INTO channels (id,name,description,created_by,created_at) VALUES (?,?,?,?,?)",
            (cid, name, desc, user["id"], datetime.now().isoformat()))
        db().execute("INSERT INTO channel_members VALUES (?,?)", (cid, user["id"]))
        db().commit()
    log("CHANNEL", f"@{user['username']} created #{name}", _CY)
    return jsonify({"status":"created","channel_id":cid,"name":name}), 201

@app.route("/channel/<name>/join", methods=["POST"])
def join_channel(name):
    with _db_lock:
        user = auth_user()
        ch   = db().execute("SELECT * FROM channels WHERE name=?", (name,)).fetchone()
        if not ch: return jsonify({"error":f"Channel #{name} not found."}), 404
        if db().execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",
                (ch["id"], user["id"])).fetchone():
            db().execute("DELETE FROM channel_members WHERE channel_id=? AND user_id=?",
                (ch["id"], user["id"]))
            action = "left"
        else:
            db().execute("INSERT INTO channel_members VALUES (?,?)", (ch["id"], user["id"]))
            action = "joined"
        db().commit()
    log("CHANNEL", f"@{user['username']} {action} #{name}", _CY)
    return jsonify({"status":action,"channel":name})

@app.route("/channel/<name>/feed", methods=["GET"])
def channel_feed(name):
    viewer = auth_user(required=False)
    vid    = viewer["id"] if viewer else None
    ch     = db().execute("SELECT * FROM channels WHERE name=?", (name,)).fetchone()
    if not ch: return jsonify({"error":f"Channel #{name} not found."}), 404
    page = max(1, int(request.args.get("page", 1))); per = 20
    rows = db().execute(
        "SELECT * FROM posts WHERE channel_id=? AND parent_id IS NULL ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        (ch["id"], per+1, (page-1)*per)).fetchall()
    has_more = len(rows) > per
    return jsonify({"channel":name,"posts":[post_to_dict(r,vid) for r in rows[:per]],
                    "page":page,"has_more":has_more})

@app.route("/channel/<name>/post", methods=["POST"])
def post_to_channel(name):
    with _db_lock:
        user = auth_user()
        ch   = db().execute("SELECT * FROM channels WHERE name=?", (name,)).fetchone()
        if not ch: return jsonify({"error":f"Channel #{name} not found."}), 404
        if not db().execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",
                (ch["id"], user["id"])).fetchone():
            db().execute("INSERT INTO channel_members VALUES (?,?)", (ch["id"], user["id"]))
        text = (request.get_json(force=True).get("text") or "").strip()
        if not text: return jsonify({"error":"Post text cannot be empty."}), 400
        if len(text) > 500: return jsonify({"error":"Post too long (max 500 chars)."}), 400
        if _rate_limit(_rl_post, user["id"], 20, 60):
            return jsonify({"error":"Slow down! Max 20 posts per minute."}), 429
        pid = next_id("post")
        db().execute(
            "INSERT INTO posts (id,user_id,username,text,timestamp,parent_id,channel_id) VALUES (?,?,?,?,?,NULL,?)",
            (pid, user["id"], user["username"], text, datetime.now().isoformat(), ch["id"]))
        db().commit()
        push_notifs_for_text(user, text, pid, "post")
    log("POST", f"@{user['username']} #{name} {pid}  {text[:40]}", _YL)
    return jsonify({"post_id":pid,"post":post_to_dict(
        db().execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone(), user["id"])}), 201

# ── announcements ────────────────────────────────────────────────────────────────
@app.route("/admin/announce", methods=["POST"])
def admin_announce():
    user = auth_user(); require_admin(user)
    text = (request.get_json(force=True).get("text") or "").strip()
    if not text: return jsonify({"error":"Announcement text required."}), 400
    if len(text) > 500: return jsonify({"error":"Max 500 chars."}), 400
    with _db_lock:
        db().execute(
            "INSERT INTO announcements (admin_id,admin_user,text,ts) VALUES (?,?,?,?)",
            (user["id"], user["username"], text, datetime.now().isoformat()))
        db().commit()
    all_users = db().execute("SELECT id FROM users WHERE id != ?", (user["id"],)).fetchall()
    for u in all_users:
        push_notif(u["id"], "announce", user, "0", text)
    log("ANNOUNCE", f"@{user['username']}: {text[:50]}", _YL)
    return jsonify({"status":"sent","recipients":len(all_users)})

@app.route("/announcements", methods=["GET"])
def get_announcements():
    rows = db().execute(
        "SELECT * FROM announcements ORDER BY ts DESC LIMIT 20").fetchall()
    return jsonify({"announcements":[dict(r) for r in rows]})

# ── notifications ───────────────────────────────────────────────────────────────
@app.route("/notifications")
def get_notifications():
    user = auth_user()
    rows = db().execute(
        "SELECT * FROM notifications WHERE target_id=? ORDER BY ts DESC LIMIT 50",
        (user["id"],)).fetchall()
    notifs = [dict(r) for r in rows]
    for n in notifs: n["from"] = n.pop("from_user"); n["post"] = n.pop("post_ref")
    unread = sum(1 for n in notifs if not n["read"])
    return jsonify({"notifications":notifs,"unread":unread})

@app.route("/notifications/read", methods=["POST"])
def mark_read():
    with _db_lock:
        user = auth_user()
        db().execute("UPDATE notifications SET read=1 WHERE target_id=?", (user["id"],))
        db().commit()
    return jsonify({"status":"ok"})

@app.route("/notifications/poll")
def poll_notifications():
    """Long-poll: waits up to 20 s for a new notification, then returns unread count."""
    user   = auth_user()
    before = int(request.args.get("after_id", 0))
    # Check immediately first
    unread = db().execute(
        "SELECT COUNT(*) FROM notifications WHERE target_id=? AND id>? AND read=0",
        (user["id"], before)).fetchone()[0]
    if unread > 0:
        last_id = db().execute(
            "SELECT MAX(id) FROM notifications WHERE target_id=?",
            (user["id"],)).fetchone()[0] or 0
        return jsonify({"unread": unread, "last_id": last_id})
    # Wait up to 20 s
    _notify_event.wait(timeout=20)
    unread = db().execute(
        "SELECT COUNT(*) FROM notifications WHERE target_id=? AND id>? AND read=0",
        (user["id"], before)).fetchone()[0]
    last_id = db().execute(
        "SELECT MAX(id) FROM notifications WHERE target_id=?",
        (user["id"],)).fetchone()[0] or 0
    return jsonify({"unread": unread, "last_id": last_id})

# ── follow ──────────────────────────────────────────────────────────────────────
@app.route("/follow/<target_id>", methods=["POST"])
def follow(target_id):
    with _db_lock:
        user   = auth_user()
        target = db().execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
        if not target: return jsonify({"error":f"User '{target_id}' not found."}), 404
        if target_id == user["id"]: return jsonify({"error":"Cannot follow yourself."}), 400
        existing = db().execute(
            "SELECT 1 FROM follows WHERE follower_id=? AND target_id=?",
            (user["id"], target_id)).fetchone()
        if existing:
            db().execute("DELETE FROM follows WHERE follower_id=? AND target_id=?",
                         (user["id"], target_id))
            action = "unfollowed"
        else:
            db().execute("INSERT INTO follows VALUES (?,?)", (user["id"], target_id))
            action = "followed"
        db().commit()
    log("FOLLOW", f"@{user['username']} {action} {target_id}", _MG)
    return jsonify({"status":action,"target":target_id})

# ── DMs ─────────────────────────────────────────────────────────────────────────
@app.route("/dm/<target_id>", methods=["POST"])
def send_dm(target_id):
    with _db_lock:
        user   = auth_user()
        target = db().execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
        if not target: return jsonify({"error":f"User '{target_id}' not found."}), 404
        if target_id == user["id"]: return jsonify({"error":"Cannot DM yourself."}), 400
        text = (request.get_json(force=True).get("text") or "").strip()
        if not text: return jsonify({"error":"Message cannot be empty."}), 400
        if len(text) > 1000: return jsonify({"error":"Message too long (max 1000 chars)."}), 400
        mid = next_id("msg")
        db().execute("""INSERT INTO messages
            (id,from_id,from_user,to_id,to_user,text,timestamp)
            VALUES (?,?,?,?,?,?,?)""",
            (mid, user["id"], user["username"], target_id, target["username"],
             text, datetime.now().isoformat()))
        db().commit()
        push_notif(target_id, "dm", user, mid, text)
    log("DM", f"@{user['username']} -> @{target['username']}  {text[:30]}", _MG)
    msg = db().execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    return jsonify({"message_id":mid,"message":dict(msg)}), 201

@app.route("/dm/<target_id>", methods=["GET"])
def get_dm(target_id):
    user = auth_user()
    rows = db().execute("""
        SELECT * FROM messages
        WHERE (from_id=? AND to_id=?) OR (from_id=? AND to_id=?)
        ORDER BY timestamp""",
        (user["id"], target_id, target_id, user["id"])).fetchall()
    return jsonify({"messages":[dict(r) for r in rows]})

@app.route("/dm/inbox", methods=["GET"])
def dm_inbox():
    user = auth_user(); uid = user["id"]
    rows = db().execute("""
        SELECT * FROM messages
        WHERE from_id=? OR to_id=?
        ORDER BY timestamp DESC""", (uid, uid)).fetchall()
    seen = {}
    for m in rows:
        other_id   = m["to_id"]   if m["from_id"]==uid else m["from_id"]
        other_name = m["to_user"] if m["from_id"]==uid else m["from_user"]
        if other_id not in seen:
            seen[other_id] = {"with_id":other_id,"with":other_name,
                              "last_msg":m["text"][:60],"timestamp":m["timestamp"],
                              "count":1}
        else:
            seen[other_id]["count"] += 1
    return jsonify({"conversations":sorted(seen.values(), key=lambda c:c["timestamp"], reverse=True)})

# ── admin ───────────────────────────────────────────────────────────────────────
@app.route("/admin/stats", methods=["GET"])
def admin_stats():
    user = auth_user()
    require_admin(user)

    conn = db()

    full = request.args.get("full", "false").lower() == "true"

    # ── counts ─────────────────────────────────────────────
    total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    banned = conn.execute("SELECT COUNT(*) FROM users WHERE is_banned=1").fetchone()[0]

    total_posts = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]

    total_likes = conn.execute("SELECT COUNT(*) FROM likes").fetchone()[0]

    total_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    total_notifications = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]

    # ── top posters ────────────────────────────────────────
    top_posters = [
        {
            "username": r["username"],
            "cnt": r["cnt"]
        }
        for r in conn.execute("""
            SELECT username, COUNT(*) as cnt
            FROM posts
            GROUP BY user_id
            ORDER BY cnt DESC
            LIMIT 5
        """).fetchall()
    ]

    # ── recent users ───────────────────────────────────────
    recent_users = [
        {
            "id": r["id"],
            "username": r["username"],
            "joined": r["joined"],
            "is_admin": bool(r["is_admin"]),
            "is_banned": bool(r["is_banned"])
        }
        for r in conn.execute("""
            SELECT id, username, joined, is_admin, is_banned
            FROM users
            ORDER BY joined DESC
            LIMIT 5
        """).fetchall()
    ]

    # ── base response ──────────────────────────────────────
    response = {
        "users": total_users,
        "banned": banned,
        "posts": total_posts,
        "likes": total_likes,
        "messages": total_messages,
        "notifications": total_notifications,
        "top_posters": top_posters,
        "recent_users": recent_users
    }

    # ── FULL MODE ──────────────────────────────────────────
    if full:
        posts_per_user = {
            r["user_id"]: r["cnt"]
            for r in conn.execute("""
                SELECT user_id, COUNT(*) as cnt
                FROM posts
                GROUP BY user_id
            """).fetchall()
        }

        top_liked_posts = [
            {
                "post_id": r["post_id"],
                "likes": r["cnt"]
            }
            for r in conn.execute("""
                SELECT post_id, COUNT(*) as cnt
                FROM likes
                GROUP BY post_id
                ORDER BY cnt DESC
                LIMIT 10
            """).fetchall()
        ]

        conversation_sizes = {
            r["pair"]: r["cnt"]
            for r in conn.execute("""
                SELECT 
                    CASE 
                        WHEN from_id < to_id THEN from_id || ':' || to_id
                        ELSE to_id || ':' || from_id
                    END as pair,
                    COUNT(*) as cnt
                FROM messages
                GROUP BY pair
            """).fetchall()
        }

        notifications_per_user = {
            r["target_id"]: r["cnt"]
            for r in conn.execute("""
                SELECT target_id, COUNT(*) as cnt
                FROM notifications
                GROUP BY target_id
            """).fetchall()
        }

        response["full"] = {
            "posts_per_user": posts_per_user,
            "top_liked_posts": top_liked_posts,
            "conversation_sizes": conversation_sizes,
            "notifications_per_user": notifications_per_user
        }

    return jsonify(response)

@app.route("/admin/ban/<user_id>", methods=["POST"])
def admin_ban(user_id):
    actor = auth_user(); require_admin(actor)
    target = db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not target: return jsonify({"error":"User not found."}), 404
    if target["is_admin"]: return jsonify({"error":"Cannot ban another admin."}), 403
    new_val = 0 if target["is_banned"] else 1
    with _db_lock:
        db().execute("UPDATE users SET is_banned=? WHERE id=?", (new_val, user_id))
        db().commit()
    action = "unbanned" if new_val==0 else "banned"
    log("ADMIN", f"@{actor['username']} {action} @{target['username']}", _RD)
    return jsonify({"status":action,"user_id":user_id,"username":target["username"]})

@app.route("/admin/delete/post/<post_id>", methods=["POST"])
def admin_delete_post(post_id):
    actor = auth_user(); require_admin(actor)
    post = db().execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if not post: return jsonify({"error":"Post not found."}), 404
    with _db_lock:
        db().execute("DELETE FROM likes WHERE post_id=?", (post_id,))
        db().execute("DELETE FROM posts WHERE id=?", (post_id,))
        db().commit()
    log("ADMIN", f"@{actor['username']} deleted post {post_id}", _RD)
    return jsonify({"status":"deleted","post_id":post_id})

@app.route("/admin/delete/user/<user_id>", methods=["POST"])
def admin_delete_user(user_id):
    actor = auth_user(); require_admin(actor)
    if user_id == actor["id"]: return jsonify({"error":"Cannot delete yourself."}), 400
    target = db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not target: return jsonify({"error":"User not found."}), 404
    with _db_lock:
        db().execute("DELETE FROM follows WHERE follower_id=? OR target_id=?", (user_id,user_id))
        db().execute("DELETE FROM likes WHERE user_id=?", (user_id,))
        db().execute("DELETE FROM notifications WHERE target_id=? OR from_id=?", (user_id,user_id))
        db().execute("DELETE FROM messages WHERE from_id=? OR to_id=?", (user_id,user_id))
        db().execute("DELETE FROM posts WHERE user_id=?", (user_id,))
        db().execute("DELETE FROM users WHERE id=?", (user_id,))
        db().commit()
    log("ADMIN", f"@{actor['username']} deleted user {user_id}", _RD)
    return jsonify({"status":"deleted","user_id":user_id})

@app.route("/admin/make_admin/<user_id>", methods=["POST"])
def admin_promote(user_id):
    actor = auth_user(); require_admin(actor)
    target = db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not target: return jsonify({"error":"User not found."}), 404
    with _db_lock:
        db().execute("UPDATE users SET is_admin=1 WHERE id=?", (user_id,))
        db().commit()
    log("ADMIN", f"@{actor['username']} promoted @{target['username']}", _YL)
    return jsonify({"status":"promoted","user_id":user_id,"username":target["username"]})

@app.route("/admin/users")
def admin_list_users():
    actor = auth_user(); require_admin(actor)
    rows = db().execute(
        "SELECT id,username,joined,is_admin,is_banned FROM users ORDER BY joined"
    ).fetchall()
    return jsonify({"users":[dict(r) for r in rows]})

# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════
def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8",80)); ip=s.getsockname()[0]; s.close(); return ip
    except: return "127.0.0.1"

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5151)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args(); ip = local_ip()

    # Init DB
    db()
    upgrade_schema()
    maybe_migrate()

    print(f"""
{_CY}{_B} ████████╗███████╗██████╗ ███╗   ███╗██╗███╗   ██╗███████╗████████╗{_R}
{_CY}{_B}    ██╔══╝██╔════╝██╔══██╗████╗ ████║██║████╗  ██║██╔════╝╚══██╔══╝{_R}
{_CY}{_B}    ██║   █████╗  ██████╔╝██╔████╔██║██║██╔██╗ ██║█████╗     ██║   {_R}
{_CY}{_B}    ██║   ██╔══╝  ██╔══██╗██║╚██╔╝██║██║██║╚██╗██║██╔══╝     ██║   {_R}
{_CY}{_B}    ██║   ███████╗██║  ██║██║ ╚═╝ ██║██║██║ ╚████║███████╗   ██║   {_R}
{_CY}{_B}    ╚═╝   ╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝╚═╝╚═╝  ╚═══╝╚══════╝   ╚═╝  {_R}
{_GY}    LAN Server v4  ·  SQLite  ·  bcrypt={'yes' if BCRYPT_OK else 'no — pip install bcrypt'}{_R}
""")
    print(f"  {_GR}[ OK ]{_R}  Server is UP")
    print(f"  {_YL}[ IP ]{_R}  Your LAN IP  :  {_B}{ip}:{args.port}{_R}")
    print(f"  {_CY}[INFO]{_R}  Tell others:   terminet connect {ip}:{args.port}")
    print(f"  {_GY}------  {args.host}:{args.port}  (Ctrl+C to stop){_R}\n")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
