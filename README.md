# Terminet 🖥️

**The terminal social network. LAN edition.**

A lightweight, self-hosted social network that runs entirely in your terminal over a local network (or via Cloudflare Tunnel for internet access). Post, reply, like, DM, create polls, join channels, and get live notifications — all without leaving the command line.

---

## Features

- 📝 **Posts & Replies** — publish, edit, delete, and view full reply threads
- ❤️ **Likes** — toggle likes on any post
- 📊 **Polls** — create polls with 2–8 options and vote in real time
- 📢 **Channels** — create topic channels, join them, and post inside
- 🔔 **Live Notifications** — background polling in interactive mode alerts you instantly
- 💬 **Direct Messages** — private DMs with full conversation history
- 👤 **Profiles & Bios** — follow users, view their posts and stats
- 🔍 **Search** — search posts and users
- 📣 **Announcements** — admins can broadcast to all users
- 🛡️ **Admin Tools** — ban, promote, delete users/posts, view server stats
- 🔐 **bcrypt passwords** — secure hashing with SHA-256 fallback
- 💾 **SQLite backend** — persistent storage with WAL mode

---

## Requirements

- Python 3.8+
- See `requirements.txt` for dependencies

---

## Quick Start

### 1. Start the server

```bash
pip install flask bcrypt
python terminet_server.py
# Optional flags:
python terminet_server.py --port 5151 --host 0.0.0.0
```

The server will display your LAN IP on startup.

### 2. Connect a client

```bash
# Install client dependency
pip install requests

# Point at the server (do this once)
python terminet.py connect 192.168.x.x:5151

# Register an account (first user becomes admin)
python terminet.py register yourname

# Start the interactive shell
python terminet.py interactive
```

On Windows, you can also use the included `terminet.bat` launcher which handles dependency installation automatically.

---

## Command Reference

### Setup
| Command | Description |
|---|---|
| `connect <ip>:<port>` | Point client at the LAN server |
| `register <username>` | Create an account |
| `login <username>` | Log in |
| `logout` | Log out |
| `whoami` | Show current session |

### Posts
| Command | Description |
|---|---|
| `post <text>` | Publish a post (max 500 chars) |
| `reply <post_id> <text>` | Reply to a post |
| `view <post_id>` | View a post and its full reply thread |
| `edit <post_id> <text>` | Edit your own post |
| `delete <post_id>` | Delete your own post |
| `like <post_id>` | Toggle like/unlike |
| `feed [page]` | Global timeline |
| `myfeed [page]` | Feed from people you follow |

### Polls
| Command | Description |
|---|---|
| `poll "question" opt1 opt2 …` | Create a poll (2–8 options) |
| `vote <post_id> <0-7>` | Cast or change your vote |

### Channels
| Command | Description |
|---|---|
| `channels` | List all channels |
| `channel <name>` | View a channel's feed |
| `channel create <name> [desc]` | Create a new channel |
| `channel join <name>` | Join or leave a channel |
| `cpost <name> <text>` | Post into a channel |

### Social
| Command | Description |
|---|---|
| `profile <@user or id>` | View a user's profile |
| `follow <user_id>` | Follow or unfollow someone |
| `bio <text>` | Set your profile bio (max 160 chars) |
| `notifications` | View your notifications |

### DMs
| Command | Description |
|---|---|
| `dm <user_id> <text>` | Send a direct message |
| `inbox` | List your DM conversations |
| `history <user_id>` | Read a DM thread |

### Search
| Command | Description |
|---|---|
| `search <query>` | Search posts and users |
| `search users <q>` | Search users only |
| `search posts <q>` | Search posts only |

### Admin
| Command | Description |
|---|---|
| `admin stats` | Server statistics |
| `admin users` | List all users |
| `admin ban <user_id>` | Ban or unban a user |
| `admin promote <user_id>` | Grant admin to a user |
| `admin delete post <id>` | Delete any post |
| `admin delete user <id>` | Delete a user and all their data |
| `announce <text>` | Broadcast to all users |
| `announcements` | Read recent announcements |

### Live Mode
```bash
python terminet.py interactive
```
Runs an interactive shell with background notification polling. Type `help` inside for a quick reference.

---

## Internet Access via Cloudflare Tunnel

To expose your server to the internet without port forwarding:

```bash
# Install cloudflared, then:
cloudflared tunnel --url http://localhost:5151
```

Share the generated `*.trycloudflare.com` URL — the client auto-detects HTTPS for non-LAN addresses.

---

## Project Structure

```
terminet.py          # CLI client
terminet_server.py   # Flask server
terminet.bat         # Windows launcher (auto-installs deps)
data.db              # SQLite database (created on first run)
.terminet_config     # Client config (created on connect)
```

---

## License

SOCL
