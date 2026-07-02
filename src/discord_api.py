"""CowBot's Discord toolbox — one CLI over the Discord REST API.

Auth: reads $DISCORD_BOT_TOKEN (falls back to the .env in cwd / workspace).
Everything is scoped to what the bot's token can do in its guild.

Examples:
    python discord_api.py whoami
    python discord_api.py channels                 # text channels in the guild
    python discord_api.py threads                  # active threads (forum posts)
    python discord_api.py read   <channel_id> [--limit 30]
    python discord_api.py post   <channel_id> "text" [--reply <msg_id>] [--mention <uid> ...]
    python discord_api.py reply  <channel_id> <msg_id> "text"
    python discord_api.py react  <channel_id> <msg_id> ✅
    python discord_api.py edit   <channel_id> <msg_id> "new text"
    python discord_api.py pin    <channel_id> <msg_id>
    python discord_api.py forum-post <forum_channel_id> "title" "first message"

Channel ids and thread ids are interchangeable for read/post/react/edit/pin —
a thread is just a channel to the API.
"""
import argparse
import json
import os
import re
import sys
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

_H = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_H, ".env"))
load_dotenv(
    os.path.join(os.environ.get("DISCORD_AGENT_WORKSPACE", _H), ".env"), override=True
)

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
# Optional restriction (one id or comma-separated); blank → all joined guilds.
GUILDS = [g.strip() for g in os.environ.get("DISCORD_GUILD_ID", "").split(",") if g.strip()]
API = "https://discord.com/api/v10"
H = {"Authorization": f"Bot {TOKEN}"}
HJSON = {**H, "Content-Type": "application/json"}
TEXT_TYPES = {0, 5, 10, 11, 12, 15}


def _req(method, path, **kw):
    r = httpx.request(method, API + path, timeout=20, **kw)
    if r.status_code >= 400:
        raise SystemExit(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
    return r


def _guilds():
    # During message handling the bridge sets AGENT_CURRENT_GUILD so the toolbox
    # is scoped to ONLY the current server (server isolation — never expose other
    # servers' channels/threads here).
    current = os.environ.get("AGENT_CURRENT_GUILD", "").strip()
    if current:
        return [(current, None)]
    if GUILDS:
        return [(g, None) for g in GUILDS]
    return [(str(g["id"]), g.get("name")) for g in _req("GET", "/users/@me/guilds", headers=H).json()]


def whoami(_):
    me = _req("GET", "/users/@me", headers=H).json()
    gs = ", ".join(f"{name or gid}" for gid, name in _guilds())
    print(f"{me['username']} (id={me['id']}) guilds: {gs}")


def channels(_):
    for gid, name in _guilds():
        print(f"# guild {name or gid}")
        for c in _req("GET", f"/guilds/{gid}/channels", headers=H).json():
            if c.get("type") in TEXT_TYPES:
                print(f"{c['id']}  type={c['type']:<2} {c.get('name')}")


def threads(_):
    for gid, name in _guilds():
        for t in _req("GET", f"/guilds/{gid}/threads/active", headers=H).json().get("threads", []):
            print(f"{t['id']}  parent={t.get('parent_id')}  {t.get('name')}")


def read(a):
    msgs = _req(
        "GET", f"/channels/{a.channel}/messages", headers=H, params={"limit": a.limit}
    ).json()
    for m in sorted(msgs, key=lambda x: int(x["id"])):
        body = " ".join((m.get("content") or "").split())
        print(f"{m['id']} [{m['author']['username']}] {body}")


def _send(channel, content, reply=None, mentions=None):
    # Whitelist both explicitly-passed --mention ids AND any <@id> already in the
    # text, so a mention written inline actually pings (Discord drops mentions
    # not in allowed_mentions). Empty list => nothing pings.
    ids = set(mentions or []) | set(re.findall(r"<@!?(\d+)>", content))
    payload = {
        "content": content[:2000],
        "allowed_mentions": {"users": sorted(ids)},
    }
    if reply:
        payload["message_reference"] = {"message_id": reply}
    mid = _req("POST", f"/channels/{channel}/messages", headers=HJSON,
               json=payload).json()["id"]
    print(mid)


def post(a):
    _send(a.channel, a.text, mentions=a.mention)


def reply(a):
    _send(a.channel, a.text, reply=a.message, mentions=a.mention)


def react(a):
    emoji = quote(a.emoji)
    _req("PUT", f"/channels/{a.channel}/messages/{a.message}/reactions/{emoji}/@me",
         headers=H)
    print("ok")


def edit(a):
    _req("PATCH", f"/channels/{a.channel}/messages/{a.message}", headers=HJSON,
         json={"content": a.text[:2000]})
    print("ok")


def pin(a):
    _req("PUT", f"/channels/{a.channel}/pins/{a.message}", headers=H)
    print("ok")


def forum_post(a):
    t = _req("POST", f"/channels/{a.forum}/threads", headers=HJSON,
             json={"name": a.title[:100], "message": {"content": a.text[:2000]}}).json()
    print(t["id"])


def main():
    p = argparse.ArgumentParser(description="CowBot Discord toolbox")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami").set_defaults(fn=whoami)
    sub.add_parser("channels").set_defaults(fn=channels)
    sub.add_parser("threads").set_defaults(fn=threads)

    r = sub.add_parser("read"); r.add_argument("channel"); r.add_argument("--limit", type=int, default=30); r.set_defaults(fn=read)
    po = sub.add_parser("post"); po.add_argument("channel"); po.add_argument("text"); po.add_argument("--reply"); po.add_argument("--mention", action="append", default=[]); po.set_defaults(fn=lambda a: _send(a.channel, a.text, a.reply, a.mention))
    rp = sub.add_parser("reply"); rp.add_argument("channel"); rp.add_argument("message"); rp.add_argument("text"); rp.add_argument("--mention", action="append", default=[]); rp.set_defaults(fn=reply)
    re_ = sub.add_parser("react"); re_.add_argument("channel"); re_.add_argument("message"); re_.add_argument("emoji"); re_.set_defaults(fn=react)
    ed = sub.add_parser("edit"); ed.add_argument("channel"); ed.add_argument("message"); ed.add_argument("text"); ed.set_defaults(fn=edit)
    pn = sub.add_parser("pin"); pn.add_argument("channel"); pn.add_argument("message"); pn.set_defaults(fn=pin)
    fp = sub.add_parser("forum-post"); fp.add_argument("forum"); fp.add_argument("title"); fp.add_argument("text"); fp.set_defaults(fn=forum_post)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
