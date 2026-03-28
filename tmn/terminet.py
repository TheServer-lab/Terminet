#!/usr/bin/env python3
"""
terminet.py  —  Terminet CLI Client  v5
  pip install requests
  terminet connect 192.168.x.x:5151
  terminet register yourname
  terminet interactive          ← live mode

  New in v5:
    • bio <text>               — set your profile bio (max 160 chars)
    • edit <post_id> <text>    — edit your own post
    • delete <post_id>         — delete your own post
    • poll "question" opt1 opt2 [opt3…]  — create a poll (2-8 options)
    • vote <post_id> <0-7>     — vote on a poll
    • channels                 — list all channels
    • channel create <name>    — create a channel
    • channel join <name>      — join/leave a channel
    • channel <name>           — view channel feed
    • cpost <name> <text>      — post into a channel
    • announce <text>          — broadcast to all users (admin only)
    • announcements            — read recent announcements
"""

import sys, json, os, textwrap, re, threading, time
try:
    import requests
except ImportError:
    print("\n  [ERROR]  Missing dependency.  Run:  pip install requests\n")
    sys.exit(1)

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, ".terminet_config")

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
R  =_c("\033[0m");  B  =_c("\033[1m");  DIM=_c("\033[2m")
CY =_c("\033[96m"); GR =_c("\033[92m"); YL =_c("\033[93m")
RD =_c("\033[91m"); MG =_c("\033[95m"); BL =_c("\033[94m")
GY =_c("\033[90m"); IT =_c("\033[3m")
WIDTH = 68

# ── config ─────────────────────────────────────────────────────────────────────
def load_cfg():
    if not os.path.exists(CONFIG_FILE): return {}
    with open(CONFIG_FILE) as f: return json.load(f)

def save_cfg(c):
    with open(CONFIG_FILE, "w") as f: json.dump(c, f, indent=2)

def get_server():
    u = load_cfg().get("server")
    if not u: err("No server set.  Run:  terminet connect <ip>:<port>"); sys.exit(1)
    return u.rstrip("/")

def auth_headers():
    t = load_cfg().get("token")
    return {"Authorization": f"Bearer {t}"} if t else {}

# ── display ────────────────────────────────────────────────────────────────────
def div(c="─"): print(f"{GY}{c*WIDTH}{R}")
def header(t):
    print(); div("═")
    pad = (WIDTH-len(t)-2)//2
    print(f"{GY}║{R}{' '*pad}{B}{CY}{t}{R}{' '*(WIDTH-pad-len(t)-2)}{GY}║{R}")
    div("═")

def ok(m):   print(f"  {GR}[ OK ]{R}  {m}")
def err(m):  print(f"  {RD}[ERR]{R}  {m}")
def info(m): print(f"  {CY}[INFO]{R}  {m}")
def warn(m): print(f"  {YL}[WARN]{R}  {m}")

def hl(t): return re.sub(r"(@\w+)", f"{MG}\\1{R}", t)

def heart(n): return f"{RD}♥{R}{YL}{n}{R}" if n else f"{GY}♡ {n}{R}"

def render_post(post, indent=0):
    pad = "  "*indent
    ts  = (post.get("timestamp","") or "")[:16].replace("T"," ")
    txt = textwrap.fill(post.get("text",""), width=WIDTH-6-indent*2)
    likes = post.get("like_count", len(post.get("likes",[])))
    liked = " ♥" if post.get("liked_by_me") else ""
    edited = f" {GY}(edited){R}" if post.get("edited") else ""
    ch = f" {BL}#{post['channel_id']}{R}" if post.get("channel_id") else ""
    print(f"{pad}{B}{BL}@{post['username']}{R} {GY}[{post['user_id']}]{R}  "
          f"{DIM}{ts}{R}{edited}{ch}  {YL}#{post['id']}{R}  {heart(likes)}{RD}{liked}{R}")
    for line in hl(txt).split("\n"): print(f"{pad}  {line}")
    # poll rendering
    poll = post.get("poll")
    if poll:
        total = poll["total"] or 1
        my    = poll.get("my_vote")
        print(f"{pad}  {B}{YL}Poll:{R} {poll['question']}")
        for i, opt in enumerate(poll["options"]):
            cnt  = poll["votes"][i]
            pct  = int(cnt/total*100) if poll["total"] else 0
            bar  = "█" * (pct//5)
            sel  = f" {GR}← your vote{R}" if my == i else ""
            print(f"{pad}    {GY}[{i}]{R} {opt:<22} {YL}{bar:<20}{R} {pct:3}% ({cnt}){sel}")
        print(f"{pad}  {GY}{poll['total']} vote(s) total{R}")
    replies = post.get("replies",[])
    n = len(replies)
    if n: print(f"{pad}  {GY}↩ {n} repl{'y' if n==1 else 'ies'}{R}")
    div()

def safe_json(r):
    try: return r.json()
    except: return {"error": r.text.strip()[:300] or f"HTTP {r.status_code}"}

def api(method, path, **kw):
    try:
        return requests.request(method, f"{get_server()}{path}", timeout=8, **kw)
    except requests.exceptions.ConnectionError:
        err(f"Cannot reach server at {get_server()}"); sys.exit(1)
    except requests.exceptions.Timeout:
        err("Request timed out."); sys.exit(1)

# ── commands ───────────────────────────────────────────────────────────────────
def _resolve_address(address):
    """Auto-detect http vs https.
    Local IPs / localhost with explicit port → http://
    Everything else (hostnames, Cloudflare tunnels, etc.) → https://
    """
    if address.startswith("http://") or address.startswith("https://"):
        return address
    import re
    # bare IP with port, or localhost — treat as plain HTTP
    if re.match(r"^(\d{1,3}\.){3}\d{1,3}(:\d+)?$", address) \
            or address.startswith("localhost") \
            or address.startswith("127.") \
            or address.startswith("192.168.") \
            or address.startswith("10.") \
            or address.startswith("172."):
        return "http://" + address
    # anything else (e.g. xyz.trycloudflare.com) → HTTPS
    return "https://" + address

def cmd_connect(address):
    address = _resolve_address(address)
    print(f"\n  {CY}Pinging {address} ...{R}")
    try:
        r = requests.get(f"{address}/ping", timeout=5); r.raise_for_status()
        d = r.json()
    except Exception as e: err(f"Could not connect: {e}"); sys.exit(1)
    cfg = load_cfg(); cfg["server"] = address; save_cfg(cfg)
    ok(f"Connected to  {B}{YL}{address}{R}")
    info(f"Server v{d.get('version','?')}  ·  {d.get('users',0)} users  ·  {d.get('posts',0)} posts")
    print()

def cmd_register(username):
    print(f"\n  {CY}Creating account for {B}@{username}{R}")
    import getpass
    pw = getpass.getpass("  Password: "); pw2 = getpass.getpass("  Confirm : ")
    if pw != pw2: err("Passwords don't match."); sys.exit(1)
    r = api("POST", "/register", json={"username":username,"password":pw})
    b = safe_json(r)
    if r.status_code != 201:
        err(f"Registration failed: {b.get('error', r.text[:200])}"); sys.exit(1)
    cfg = load_cfg()
    cfg.update({"token":b["token"],"username":b["username"],"user_id":b["user_id"],
                "is_admin":b.get("is_admin",False)})
    save_cfg(cfg)
    ok(f"Welcome to Terminet, {B}{CY}@{b['username']}{R}!")
    info(f"Your user ID is {B}{YL}{b['user_id']}{R}")
    if b.get("is_admin"): info(f"{YL}You are the first user — granted {B}ADMIN{R}{YL} privileges!{R}")
    print()

def cmd_login(username):
    import getpass
    pw = getpass.getpass(f"  Password for @{username}: ")
    r  = api("POST", "/login", json={"username":username,"password":pw})
    b  = safe_json(r)
    if r.status_code != 200:
        err(f"Login failed: {b.get('error', r.text[:200])}"); sys.exit(1)
    cfg = load_cfg()
    cfg.update({"token":b["token"],"username":b["username"],"user_id":b["user_id"],
                "is_admin":b.get("is_admin",False)})
    save_cfg(cfg)
    ok(f"Logged in as {B}{CY}@{b['username']}{R}  ({YL}{b['user_id']}{R})")
    if b.get("is_admin"): info(f"Admin mode active.")
    print()

def cmd_logout():
    cfg = load_cfg()
    if not cfg.get("token"): warn("Not logged in."); return
    api("POST", "/logout", headers=auth_headers())
    u = cfg.pop("username","?"); cfg.pop("token",None)
    cfg.pop("user_id",None); cfg.pop("is_admin",None); save_cfg(cfg)
    ok(f"Logged out of @{u}."); print()

def cmd_whoami():
    cfg = load_cfg()
    if not cfg.get("token"): warn("Not logged in.")
    else:
        ok(f"Logged in as {B}{CY}@{cfg['username']}{R}  ({YL}{cfg['user_id']}{R})")
        info(f"Server: {YL}{cfg.get('server','?')}{R}")
        if cfg.get("is_admin"): info(f"{YL}Admin privileges active.{R}")
    print()

def cmd_post(text):
    r = api("POST", "/post", json={"text":text}, headers=auth_headers())
    b = safe_json(r)
    if r.status_code == 401: err("Not logged in.  →  terminet login <username>"); sys.exit(1)
    if r.status_code != 201: err(b.get("error","Post failed.")); sys.exit(1)
    print(); ok(f"Posted!  {YL}#{b['post_id']}{R}"); div()
    render_post(b["post"])

def cmd_reply(post_id, text):
    r = api("POST", f"/reply/{post_id}", json={"text":text}, headers=auth_headers())
    b = safe_json(r)
    if r.status_code == 401: err("Not logged in."); sys.exit(1)
    if not r.ok: err(b.get("error","Reply failed.")); sys.exit(1)
    print(); ok(f"Reply posted!  {YL}#{b['reply_id']}{R}  to {YL}#{post_id}{R}"); div()
    render_post(b["post"], indent=1)

def cmd_like(post_id):
    r = api("POST", f"/like/{post_id}", headers=auth_headers())
    b = safe_json(r)
    if r.status_code == 401: err("Not logged in."); sys.exit(1)
    if not r.ok: err(b.get("error","Like failed.")); sys.exit(1)
    action = b.get("status","?")
    sym    = f"{RD}♥{R}" if action=="liked" else f"{GY}♡{R}"
    ok(f"{sym}  {B}{YL}#{post_id}{R}  —  {YL}{b['like_count']}{R} like(s) total"); print()

def cmd_feed(page=1, following=False):
    path = f"/feed/following?page={page}" if following else f"/feed?page={page}"
    r = api("GET", path, headers=auth_headers())
    b = safe_json(r)
    title = "FOLLOWING FEED" if following else f"TIMELINE  (page {b.get('page',page)})"
    header(title)
    posts = b.get("posts",[])
    if not posts: warn("Nothing here yet — be the first to post!")
    else:
        for p in posts: render_post(p)
        if b.get("has_more"):
            info(f"More posts available  →  terminet feed {page+1}")
    print()

def cmd_profile(identifier):
    # Support @username or user ID
    if identifier.startswith("@"):
        path = f"/profile/by/{identifier[1:]}"
    elif re.match(r"^U\d+$", identifier, re.I):
        path = f"/profile/{identifier}"
    else:
        # Try as username anyway (without @)
        path = f"/profile/by/{identifier}"
    r = api("GET", path, headers=auth_headers())
    b = safe_json(r)
    if not r.ok: err(b.get("error","Not found.")); sys.exit(1)
    user = b["user"]
    posts = [p for p in b["posts"] if not p.get("parent")]
    header(f"@{user['username']}")
    print(f"  {B}User ID       :{R}  {YL}{user['id']}{R}")
    if user.get("bio"): print(f"  {B}Bio           :{R}  {IT}{user['bio']}{R}")
    print(f"  {B}Joined        :{R}  {DIM}{(user.get('joined') or '')[:10]}{R}")
    print(f"  {B}Posts         :{R}  {GR}{len(posts)}{R}")
    print(f"  {B}Following     :{R}  {GR}{len(user.get('following',[]))}{R}")
    print(f"  {B}Followers     :{R}  {GR}{user.get('followers_count',0)}{R}")
    if user.get("is_admin"):  print(f"  {B}Role          :{R}  {YL}Admin{R}")
    if user.get("is_banned"): print(f"  {B}Status        :{R}  {RD}BANNED{R}")
    if user.get("is_followed_by_me"): print(f"  {B}You follow    :{R}  {GR}Yes ✓{R}")
    div()
    if not posts: warn("No posts yet.")
    for p in posts: render_post(p)
    print()

def cmd_notifications():
    r = api("GET", "/notifications", headers=auth_headers())
    b = safe_json(r)
    if r.status_code == 401: err("Not logged in."); sys.exit(1)
    notifs = b.get("notifications",[]); unread = b.get("unread",0)
    header(f"NOTIFICATIONS  ({unread} unread)")
    if not notifs: warn("No notifications yet.")
    for n in notifs:
        dot   = f"{YL}●{R} " if not n.get("read") else f"{GY}○{R} "
        kind  = n["type"].upper()
        ts    = (n.get("ts","") or "")[:16].replace("T"," ")
        color = {"mention":MG,"reply":CY,"dm":GR,"like":RD}.get(n["type"], GY)
        print(f"  {dot}{color}{kind:<8}{R}  from {B}@{n['from']}{R}  {DIM}{ts}{R}  #{n['post']}")
        print(f"      {GY}{(n.get('text','') or '')[:60]}{R}")
    div()
    api("POST", "/notifications/read", headers=auth_headers())
    print()

def cmd_follow(user_id):
    r = api("POST", f"/follow/{user_id}", headers=auth_headers())
    b = safe_json(r)
    if not r.ok: err(b.get("error","Follow failed.")); sys.exit(1)
    action = b.get("status","?")
    sym    = f"{GR}+{R}" if action=="followed" else f"{RD}-{R}"
    ok(f"You {sym} {B}@{user_id}{R}  ({action})"); print()

def cmd_dm(target_id, text):
    r = api("POST", f"/dm/{target_id}", json={"text":text}, headers=auth_headers())
    b = safe_json(r)
    if not r.ok: err(b.get("error","DM failed.")); sys.exit(1)
    ok(f"DM sent to {B}{YL}{target_id}{R}  #{b['message_id']}")
    div()
    msg = b["message"]
    ts  = (msg.get("timestamp","") or "")[:16].replace("T"," ")
    print(f"  {B}{BL}@{msg['from_user']}{R} {GY}→{R} {B}{BL}@{msg['to_user']}{R}  {DIM}{ts}{R}")
    print(f"  {hl(msg['text'])}"); div(); print()

def cmd_inbox():
    r = api("GET", "/dm/inbox", headers=auth_headers())
    b = safe_json(r)
    if not r.ok: err(b.get("error","Failed.")); sys.exit(1)
    convos = b.get("conversations",[])
    header("DM INBOX")
    if not convos: warn("No conversations yet.")
    for c in convos:
        ts = (c.get("timestamp","") or "")[:16].replace("T"," ")
        print(f"  {B}{BL}@{c['with']}{R} {GY}[{c['with_id']}]{R}  {DIM}{ts}{R}  ({c['count']} msgs)")
        print(f"    {GY}{c['last_msg']}{R}")
        div()
    print()

def cmd_history(target_id):
    r = api("GET", f"/dm/{target_id}", headers=auth_headers())
    b = safe_json(r)
    if not r.ok: err(b.get("error","Failed.")); sys.exit(1)
    msgs = b.get("messages",[])
    header(f"DM THREAD  with {target_id}")
    if not msgs: warn("No messages yet.")
    cfg = load_cfg(); me = cfg.get("user_id","")
    for msg in msgs:
        ts   = (msg.get("timestamp","") or "")[:16].replace("T"," ")
        mine = msg["from_id"] == me
        col  = BL if mine else MG
        side = f"{B}{col}@{msg['from_user']}{R}" if mine else f"{col}@{msg['from_user']}{R}"
        print(f"  {side}  {DIM}{ts}{R}")
        print(f"    {hl(msg['text'])}"); div()
    print()

def cmd_search(query, kind="all"):
    r = api("GET", f"/search?q={requests.utils.quote(query)}&type={kind}", headers=auth_headers())
    b = safe_json(r)
    if not r.ok: err(b.get("error","Search failed.")); sys.exit(1)
    header(f'SEARCH "{query}"')
    if "users" in b:
        users = b["users"]
        print(f"\n  {B}{GY}USERS  ({len(users)} found){R}")
        if not users: warn("No users match.")
        for u in users:
            admin = f"  {YL}[admin]{R}" if u.get("is_admin") else ""
            print(f"    {B}{CY}@{u['username']}{R} {GY}[{u['id']}]{R}{admin}")
        div()
    if "posts" in b:
        posts = b["posts"]
        print(f"\n  {B}{GY}POSTS  ({len(posts)} found){R}")
        if not posts: warn("No posts match.")
        else:
            for p in posts: render_post(p)
    print()

# ── admin commands ──────────────────────────────────────────────────────────────
def cmd_admin(sub, args):
    if sub == "stats":
        r = api("GET", "/admin/stats", headers=auth_headers())
        b = safe_json(r)
        if not r.ok: err(b.get("error","Failed.")); sys.exit(1)
        header("ADMIN  STATS")
        print(f"  {B}Users      :{R}  {GR}{b.get('users','?')}{R}   {RD}({b.get('banned','?')} banned){R}")
        print(f"  {B}Posts      :{R}  {GR}{b.get('posts','?')}{R}")
        print(f"  {B}Likes      :{R}  {RD}{b.get('likes','?')}{R}")
        print(f"  {B}Messages   :{R}  {BL}{b.get('messages','?')}{R}")
        print(f"  {B}Notifs     :{R}  {GY}{b.get('notifications','?')}{R}")
        div()
        print(f"\n  {B}{GY}TOP POSTERS{R}")
        for u in b.get("top_posters",[]):
            print(f"    {CY}@{u['username']:<20}{R}  {GR}{u['cnt']}{R} posts")
        div()
        print(f"\n  {B}{GY}RECENT USERS{R}")
        for u in b.get("recent_users",[]):
            flags = ("".join([
                f" {YL}[admin]{R}" if u.get("is_admin") else "",
                f" {RD}[banned]{R}" if u.get("is_banned") else ""]))
            print(f"    {CY}@{u['username']:<20}{R} {GY}[{u['id']}]{R}  {DIM}{(u.get('joined') or '')[:10]}{R}{flags}")
        div(); print()

    elif sub == "ban":
        if not args: err("Usage: terminet admin ban <user_id>"); sys.exit(1)
        user_id = args[0]
        r = api("POST", f"/admin/ban/{user_id}", headers=auth_headers())
        b = safe_json(r)
        if not r.ok: err(b.get("error","Failed.")); sys.exit(1)
        color = RD if b["status"]=="banned" else GR
        ok(f"{color}{b['status'].upper()}{R}  @{b['username']}  ({user_id})"); print()

    elif sub == "delete":
        if len(args) < 2: err("Usage: terminet admin delete post|user <id>"); sys.exit(1)
        kind2, tid = args[0], args[1]
        if kind2 == "post":
            r = api("POST", f"/admin/delete/post/{tid}", headers=auth_headers())
        elif kind2 == "user":
            r = api("POST", f"/admin/delete/user/{tid}", headers=auth_headers())
        else:
            err("Use: admin delete post <id>  or  admin delete user <id>"); sys.exit(1)
        b = safe_json(r)
        if not r.ok: err(b.get("error","Failed.")); sys.exit(1)
        ok(f"Deleted {kind2}  {YL}{tid}{R}"); print()

    elif sub == "promote":
        if not args: err("Usage: terminet admin promote <user_id>"); sys.exit(1)
        r = api("POST", f"/admin/make_admin/{args[0]}", headers=auth_headers())
        b = safe_json(r)
        if not r.ok: err(b.get("error","Failed.")); sys.exit(1)
        ok(f"Promoted {B}{CY}@{b['username']}{R} to admin."); print()

    elif sub == "users":
        r = api("GET", "/admin/users", headers=auth_headers())
        b = safe_json(r)
        if not r.ok: err(b.get("error","Failed.")); sys.exit(1)
        header("ADMIN  USER LIST")
        for u in b.get("users",[]):
            flags = "".join([
                f" {YL}[admin]{R}" if u.get("is_admin") else "",
                f" {RD}[banned]{R}" if u.get("is_banned") else ""])
            print(f"  {CY}@{u['username']:<20}{R} {GY}[{u['id']}]{R}  {DIM}{(u.get('joined') or '')[:10]}{R}{flags}")
        div(); print()

    else:
        err(f"Unknown admin sub-command '{sub}'.  Try: stats, ban, delete, promote, users")
        sys.exit(1)

# ── bio ────────────────────────────────────────────────────────────────────────
def cmd_bio(text):
    r = api("POST", "/profile/bio", json={"bio":text}, headers=auth_headers())
    b = safe_json(r)
    if not r.ok: err(b.get("error","Failed.")); sys.exit(1)
    ok(f"Bio updated: {IT}{b['bio']}{R}"); print()

# ── edit / delete own post ─────────────────────────────────────────────────────
def cmd_edit(post_id, text):
    r = api("PATCH", f"/post/{post_id}", json={"text":text}, headers=auth_headers())
    b = safe_json(r)
    if r.status_code == 401: err("Not logged in."); sys.exit(1)
    if not r.ok: err(b.get("error","Edit failed.")); sys.exit(1)
    print(); ok(f"Post {YL}#{post_id}{R} edited."); div()
    render_post(b["post"])

def cmd_delete(post_id):
    r = api("DELETE", f"/post/{post_id}", headers=auth_headers())
    b = safe_json(r)
    if r.status_code == 401: err("Not logged in."); sys.exit(1)
    if not r.ok: err(b.get("error","Delete failed.")); sys.exit(1)
    ok(f"Post {YL}#{post_id}{R} deleted."); print()

# ── view thread ───────────────────────────────────────────────────────────────
def cmd_view(post_id):
    r = api("GET", f"/post/{post_id}", headers=auth_headers())
    b = safe_json(r)
    if not r.ok: err(b.get("error", "Post not found.")); sys.exit(1)
    thread = b.get("thread", [])
    if not thread: warn("No post found."); return
    root = thread[0]
    header(f"POST  #{root['id']}  ·  {len(thread)-1} repl{'y' if len(thread)==2 else 'ies'}")
    for item in thread:
        render_post(item, indent=item.get("depth", 0))
    print()

# ── polls ──────────────────────────────────────────────────────────────────────
def cmd_poll(question, options):
    if len(options) < 2: err("Need at least 2 options."); sys.exit(1)
    r = api("POST", "/poll", json={"question":question,"options":options}, headers=auth_headers())
    b = safe_json(r)
    if r.status_code == 401: err("Not logged in."); sys.exit(1)
    if not r.ok: err(b.get("error","Poll failed.")); sys.exit(1)
    print(); ok(f"Poll created!  {YL}#{b['post_id']}{R}"); div()
    render_post(b["post"])

def cmd_vote(post_id, option):
    r = api("POST", f"/poll/{post_id}/vote", json={"option":int(option)}, headers=auth_headers())
    b = safe_json(r)
    if r.status_code == 401: err("Not logged in."); sys.exit(1)
    if not r.ok: err(b.get("error","Vote failed.")); sys.exit(1)
    print(); ok(f"Voted on {YL}#{post_id}{R}!"); div()
    render_post(b["post"])

# ── channels ───────────────────────────────────────────────────────────────────
def cmd_channels():
    r = api("GET", "/channels", headers=auth_headers())
    b = safe_json(r)
    if not r.ok: err(b.get("error","Failed.")); sys.exit(1)
    channels = b.get("channels", [])
    header("CHANNELS")
    if not channels: warn("No channels yet. Create one: channel create <name>")
    for c in channels:
        mem  = f"{GR}✓ joined{R}" if c.get("is_member") else f"{GY}not joined{R}"
        desc = f"  {IT}{GY}{c['description']}{R}" if c.get("description") else ""
        print(f"  {B}{CY}#{c['name']}{R} {GY}[{c['id']}]{R}  {YL}{c['member_count']}{R} member(s)  {mem}{desc}")
    div(); print()

def cmd_channel(name, page=1):
    r = api("GET", f"/channel/{name}/feed?page={page}", headers=auth_headers())
    b = safe_json(r)
    if not r.ok: err(b.get("error","Failed.")); sys.exit(1)
    header(f"#{name}")
    posts = b.get("posts", [])
    if not posts: warn("No posts yet. Be the first!")
    for p in posts: render_post(p)
    if b.get("has_more"): info(f"More  →  channel {name} {page+1}")
    print()

def cmd_channel_create(name, desc=""):
    r = api("POST", "/channel/create", json={"name":name,"description":desc}, headers=auth_headers())
    b = safe_json(r)
    if r.status_code == 401: err("Not logged in."); sys.exit(1)
    if not r.ok: err(b.get("error","Failed.")); sys.exit(1)
    ok(f"Channel {B}{CY}#{b['name']}{R} created!  {GY}[{b['channel_id']}]{R}")
    info(f"You're automatically a member. Post with: cpost {b['name']} <text>"); print()

def cmd_channel_join(name):
    r = api("POST", f"/channel/{name}/join", headers=auth_headers())
    b = safe_json(r)
    if r.status_code == 401: err("Not logged in."); sys.exit(1)
    if not r.ok: err(b.get("error","Failed.")); sys.exit(1)
    action = b.get("status","?")
    sym    = f"{GR}joined{R}" if action=="joined" else f"{RD}left{R}"
    ok(f"You {sym} {B}{CY}#{name}{R}"); print()

def cmd_cpost(channel_name, text):
    r = api("POST", f"/channel/{channel_name}/post", json={"text":text}, headers=auth_headers())
    b = safe_json(r)
    if r.status_code == 401: err("Not logged in."); sys.exit(1)
    if not r.ok: err(b.get("error","Post failed.")); sys.exit(1)
    print(); ok(f"Posted to {B}{CY}#{channel_name}{R}  {YL}#{b['post_id']}{R}"); div()
    render_post(b["post"])

# ── announcements ───────────────────────────────────────────────────────────────
def cmd_announce(text):
    r = api("POST", "/admin/announce", json={"text":text}, headers=auth_headers())
    b = safe_json(r)
    if r.status_code == 403: err("Admin privileges required."); sys.exit(1)
    if not r.ok: err(b.get("error","Failed.")); sys.exit(1)
    ok(f"Announcement sent to {YL}{b['recipients']}{R} user(s)."); print()

def cmd_announcements():
    r = api("GET", "/announcements", headers=auth_headers())
    b = safe_json(r)
    if not r.ok: err(b.get("error","Failed.")); sys.exit(1)
    items = b.get("announcements", [])
    header("ANNOUNCEMENTS")
    if not items: warn("No announcements yet.")
    for a in items:
        ts = (a.get("ts","") or "")[:16].replace("T"," ")
        print(f"  {B}{YL}📢 @{a['admin_user']}{R}  {DIM}{ts}{R}")
        print(f"     {a['text']}")
        div()
    print()

# ── interactive mode ────────────────────────────────────────────────────────────
def cmd_interactive():
    cfg = load_cfg()
    if not cfg.get("token"):
        warn("Not logged in.  Some commands will be limited.")
    header("INTERACTIVE MODE  (type 'help' or 'quit')")
    info("Commands run instantly. Notifications poll in background.")

    # Background notification polling
    _last_notif_id = [0]
    _stop_poll     = threading.Event()

    def poll_thread():
        while not _stop_poll.is_set():
            try:
                r = requests.get(
                    f"{get_server()}/notifications/poll",
                    headers=auth_headers(),
                    params={"after_id": _last_notif_id[0]},
                    timeout=25)
                if r.ok:
                    d = r.json()
                    unread = d.get("unread", 0)
                    if unread > 0:
                        _last_notif_id[0] = d.get("last_id", _last_notif_id[0])
                        # Print inline alert
                        print(f"\n  {YL}🔔 {unread} new notification(s){R}  "
                              f"{GY}(type 'notifications' to read){R}\n> ", end="", flush=True)
            except Exception:
                pass
            time.sleep(1)  # tiny gap between polls

    if cfg.get("token"):
        t = threading.Thread(target=poll_thread, daemon=True)
        t.start()
    else:
        _stop_poll.set()

    SHORT_HELP = f"""
  {B}{GY}POST / SOCIAL{R}
    post <text>              reply <id> <text>        like <id>
    edit <id> <text>         delete <id>              view <id>
    feed [page]              myfeed [page]             notifications
    profile <@user or id>   follow <user_id>           bio <text>

  {B}{GY}POLLS{R}
    poll "question" opt1 opt2 [opt3…]                 vote <id> <0-7>

  {B}{GY}CHANNELS{R}
    channels                 channel <n>            channel create <n>
    channel join <n>      cpost <n> <text>

  {B}{GY}SEARCH{R}
    search <query>           search users <q>          search posts <q>

  {B}{GY}DMs{R}
    dm <user_id> <text>      inbox                     history <user_id>

  {B}{GY}ANNOUNCEMENTS{R}
    announcements            announce <text>  (admin only)

  {B}{GY}ADMIN  (if you have privileges){R}
    admin stats              admin ban <id>            admin promote <id>
    admin delete post <id>   admin delete user <id>    admin users

  {B}{GY}MISC{R}
    whoami                   help                      quit
"""

    def run_interactive_cmd(line):
        parts = line.strip().split()
        if not parts: return
        cmd = parts[0].lower()
        rest = parts[1:]

        if   cmd in ("quit","exit","q"):         _stop_poll.set(); sys.exit(0)
        elif cmd == "help":                       print(SHORT_HELP)
        elif cmd == "post":      cmd_post(" ".join(rest))
        elif cmd == "reply":
            if len(rest)<2: err("Usage: reply <post_id> <text>"); return
            cmd_reply(rest[0], " ".join(rest[1:]))
        elif cmd == "like":
            if not rest: err("Usage: like <post_id>"); return
            cmd_like(rest[0])
        elif cmd == "edit":
            if len(rest)<2: err("Usage: edit <post_id> <text>"); return
            cmd_edit(rest[0], " ".join(rest[1:]))
        elif cmd == "delete":
            if not rest: err("Usage: delete <post_id>"); return
            cmd_delete(rest[0])
        elif cmd == "view":
            if not rest: err("Usage: view <post_id>"); return
            cmd_view(rest[0])
        elif cmd == "bio":
            if not rest: err("Usage: bio <text>"); return
            cmd_bio(" ".join(rest))
        elif cmd == "poll":
            if len(rest)<3: err('Usage: poll "question" opt1 opt2 [opt3…]'); return
            cmd_poll(rest[0], rest[1:])
        elif cmd == "vote":
            if len(rest)<2: err("Usage: vote <post_id> <option_number>"); return
            cmd_vote(rest[0], rest[1])
        elif cmd == "channels":  cmd_channels()
        elif cmd == "channel":
            if not rest: err("Usage: channel <n> | create <n> | join <n>"); return
            if rest[0].lower() == "create":
                if len(rest)<2: err("Usage: channel create <n>"); return
                cmd_channel_create(rest[1], " ".join(rest[2:]))
            elif rest[0].lower() == "join":
                if len(rest)<2: err("Usage: channel join <n>"); return
                cmd_channel_join(rest[1])
            else:
                cmd_channel(rest[0], int(rest[1]) if len(rest)>1 else 1)
        elif cmd == "cpost":
            if len(rest)<2: err("Usage: cpost <channel> <text>"); return
            cmd_cpost(rest[0], " ".join(rest[1:]))
        elif cmd == "announce":
            if not rest: err("Usage: announce <text>"); return
            cmd_announce(" ".join(rest))
        elif cmd == "announcements": cmd_announcements()
        elif cmd == "feed":      cmd_feed(int(rest[0]) if rest else 1)
        elif cmd == "myfeed":    cmd_feed(int(rest[0]) if rest else 1, following=True)
        elif cmd == "profile":
            if not rest: err("Usage: profile <@username or user_id>"); return
            cmd_profile(rest[0])
        elif cmd == "follow":
            if not rest: err("Usage: follow <user_id>"); return
            cmd_follow(rest[0])
        elif cmd == "notifications": cmd_notifications()
        elif cmd == "dm":
            if len(rest)<2: err("Usage: dm <user_id> <text>"); return
            cmd_dm(rest[0], " ".join(rest[1:]))
        elif cmd == "inbox":     cmd_inbox()
        elif cmd == "history":
            if not rest: err("Usage: history <user_id>"); return
            cmd_history(rest[0])
        elif cmd == "search":
            if not rest: err("Usage: search [users|posts] <query>"); return
            if rest[0].lower() in ("users","posts"):
                if len(rest)<2: err("Usage: search users|posts <query>"); return
                cmd_search(" ".join(rest[1:]), rest[0].lower())
            else:
                cmd_search(" ".join(rest))
        elif cmd == "admin":
            if not rest: err("Usage: admin <stats|ban|delete|promote|users>"); return
            cmd_admin(rest[0].lower(), rest[1:])
        elif cmd == "whoami":    cmd_whoami()
        else:
            err(f"Unknown command '{cmd}'.  Type 'help'.")

    while True:
        try:
            line = input(f"{GY}>{R} ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); _stop_poll.set(); break
        if not line: continue
        try:
            run_interactive_cmd(line)
        except SystemExit:
            _stop_poll.set(); break
        except Exception as e:
            err(f"Unexpected error: {e}")

# ── help ───────────────────────────────────────────────────────────────────────
def cmd_help():
    header("TERMINET  v4  ·  HELP")
    sections = [
        ("SETUP",   [("connect <ip>:<port>","Point at the LAN server (do this once)")]),
        ("ACCOUNT", [("register <username>","Create account  (first user = admin)"),
                     ("login <username>","Log in"),
                     ("logout","Log out"),
                     ("whoami","Show current session"),
                     ("bio <text>","Set your profile bio (max 160 chars)")]),
        ("POSTS",   [("post <text>","Publish a post  (max 500 chars)"),
                     ("reply <post_id> <text>","Reply to a post"),
                     ("view <post_id>","View a post and its full reply thread"),
                     ("edit <post_id> <text>","Edit your own post"),
                     ("delete <post_id>","Delete your own post"),
                     ("like <post_id>","Toggle like/unlike a post"),
                     ("feed [page]","Global timeline (paginated)"),
                     ("myfeed [page]","Feed from people you follow")]),
        ("POLLS",   [('poll "question" opt1 opt2 …',"Create a poll (2-8 options)"),
                     ("vote <post_id> <0-7>","Cast or change your vote")]),
        ("CHANNELS",[("channels","List all channels"),
                     ("channel <n>","View a channel's feed"),
                     ("channel create <n> [desc]","Create a new channel"),
                     ("channel join <n>","Join or leave a channel"),
                     ("cpost <n> <text>","Post into a channel")]),
        ("SOCIAL",  [("profile <@user or id>","View profile by @username or user ID"),
                     ("follow <user_id>","Follow or unfollow someone"),
                     ("notifications","See your notifications")]),
        ("SEARCH",  [("search <query>","Search posts and users"),
                     ("search users <q>","Search users only"),
                     ("search posts <q>","Search posts only")]),
        ("DMs",     [("dm <user_id> <text>","Send a direct message"),
                     ("inbox","List your DM conversations"),
                     ("history <user_id>","Read DM thread")]),
        ("ANNOUNCE",[("announcements","Read recent server announcements"),
                     ("announce <text>","Broadcast to all users (admin only)")]),
        ("ADMIN",   [("admin stats","Server stats (admin only)"),
                     ("admin users","List all users (admin only)"),
                     ("admin ban <user_id>","Ban/unban a user"),
                     ("admin delete post <id>","Delete a post"),
                     ("admin delete user <id>","Delete user & all their data"),
                     ("admin promote <user_id>","Grant admin to user")]),
        ("LIVE",    [("interactive","Interactive shell with live notification alerts")]),
    ]
    for section, cmds in sections:
        print(f"\n  {B}{GY}{section}{R}")
        for cmd, desc in cmds:
            print(f"    {GR}terminet {B}{cmd:<40}{R}  {GY}{desc}{R}")
    print()
    info(f"Mention someone with {MG}@username{R} in any post or reply.")
    info(f"Config stored in {YL}.terminet_config{R}  ·  server stores {YL}data.db{R}")
    print()

# ── banner + main ──────────────────────────────────────────────────────────────
BANNER = f"""
{CY}{B} ████████╗███████╗██████╗ ███╗   ███╗██╗███╗   ██╗███████╗████████╗{R}
{CY}{B}    ██╔══╝██╔════╝██╔══██╗████╗ ████║██║████╗  ██║██╔════╝╚══██╔══╝{R}
{CY}{B}    ██║   █████╗  ██████╔╝██╔████╔██║██║██╔██╗ ██║█████╗     ██║   {R}
{CY}{B}    ██║   ██╔══╝  ██╔══██╗██║╚██╔╝██║██║██║╚██╗██║██╔══╝     ██║   {R}
{CY}{B}    ██║   ███████╗██║  ██║██║ ╚═╝ ██║██║██║ ╚████║███████╗   ██║   {R}
{CY}{B}    ╚═╝   ╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝╚═╝╚═╝  ╚═══╝╚══════╝   ╚═╝  {R}
{GY}    The terminal social network.  LAN edition  v4.{R}
"""

def main():
    print(BANNER)
    args = sys.argv[1:]
    if args and args[0].lower() == "terminet": args = args[1:]
    if not args: cmd_help(); return
    cmd = args[0].lower()

    def need(n):
        if len(args) <= n: err(f"Usage: terminet {cmd} — run 'terminet help'"); sys.exit(1)

    if   cmd == "connect":       need(1); cmd_connect(args[1])
    elif cmd == "register":      need(1); cmd_register(args[1])
    elif cmd == "login":         need(1); cmd_login(args[1])
    elif cmd == "logout":        cmd_logout()
    elif cmd == "whoami":        cmd_whoami()
    elif cmd == "bio":           need(1); cmd_bio(" ".join(args[1:]))
    elif cmd == "post":          need(1); cmd_post(" ".join(args[1:]))
    elif cmd == "reply":         need(2); cmd_reply(args[1], " ".join(args[2:]))
    elif cmd == "like":          need(1); cmd_like(args[1])
    elif cmd == "edit":          need(2); cmd_edit(args[1], " ".join(args[2:]))
    elif cmd == "delete":        need(1); cmd_delete(args[1])
    elif cmd == "view":          need(1); cmd_view(args[1])
    elif cmd == "poll":
        need(3)
        cmd_poll(args[1], args[2:])
    elif cmd == "vote":          need(2); cmd_vote(args[1], args[2])
    elif cmd == "channels":      cmd_channels()
    elif cmd == "channel":
        need(1)
        if args[1].lower() == "create":
            need(2); cmd_channel_create(args[2], " ".join(args[3:]))
        elif args[1].lower() == "join":
            need(2); cmd_channel_join(args[2])
        else:
            cmd_channel(args[1], int(args[2]) if len(args)>2 else 1)
    elif cmd == "cpost":         need(2); cmd_cpost(args[1], " ".join(args[2:]))
    elif cmd == "announce":      need(1); cmd_announce(" ".join(args[1:]))
    elif cmd == "announcements": cmd_announcements()
    elif cmd == "feed":          cmd_feed(int(args[1]) if len(args)>1 else 1)
    elif cmd == "myfeed":        cmd_feed(int(args[1]) if len(args)>1 else 1, following=True)
    elif cmd == "profile":       need(1); cmd_profile(args[1])
    elif cmd == "notifications": cmd_notifications()
    elif cmd == "follow":        need(1); cmd_follow(args[1])
    elif cmd == "dm":            need(2); cmd_dm(args[1], " ".join(args[2:]))
    elif cmd == "inbox":         cmd_inbox()
    elif cmd == "history":       need(1); cmd_history(args[1])
    elif cmd == "search":
        need(1)
        if args[1].lower() in ("users","posts") and len(args) > 2:
            cmd_search(" ".join(args[2:]), args[1].lower())
        else:
            cmd_search(" ".join(args[1:]))
    elif cmd == "admin":
        need(1); cmd_admin(args[1].lower(), args[2:])
    elif cmd == "interactive":   cmd_interactive()
    elif cmd in ("help","--help","-h"): cmd_help()
    else: err(f"Unknown command '{cmd}'.  Run: terminet help"); sys.exit(1)

if __name__ == "__main__":
    main()
