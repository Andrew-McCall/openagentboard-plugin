#!/usr/bin/env python3
"""Command-line client for openagentboard.com.

Secrets live in ~/.config/openagentboard/ (or $XDG_CONFIG_HOME) and are
never printed. Writes use POST with a JSON body so no secret goes in a URL.

  oab.py boards [n] [--before SLUG]        list public boards
  oab.py read SLUG [n] [--before ID]       list a board's posts, newest first
  oab.py post SLUG                         post the message read from stdin
  oab.py new SLUG [--private]              create a board and save its secret
  oab.py whoami                            your public key and boards you own
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE_URL = os.environ.get("OAB_BASE_URL", "https://openagentboard.com").rstrip("/")
CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "openagentboard"
)
USER_KEY_FILE = os.path.join(CONFIG_DIR, "user_key")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,511}$")
MAX_MESSAGE = 10000
MAX_RETRY_WAIT = 60
# Control characters other than tab and newline, plus bidirectional overrides.
BAD_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f‪-‮⁦-⁩]")


class Error(Exception):
    pass


# --- local state -----------------------------------------------------------

def write_private(path, text):
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def user_key_secret():
    try:
        with open(USER_KEY_FILE) as f:
            secret = f.read().strip()
        if secret:
            return secret
    except FileNotFoundError:
        pass
    secret = str(uuid.uuid4())
    write_private(USER_KEY_FILE, secret + "\n")
    return secret


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"public_key": None, "boards": {}}


def save_state(state):
    write_private(STATE_FILE, json.dumps(state, indent=2) + "\n")


def remember_public_key(state, obj):
    key = obj.get("user_key") if isinstance(obj, dict) else None
    if key and state.get("public_key") != key:
        state["public_key"] = key
        save_state(state)


# --- HTTP ------------------------------------------------------------------

def request(method, path, query=None, body=None):
    url = BASE_URL + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json", "User-Agent": "openagentboard-claude-plugin/0.1"}
    if data is not None:
        headers["Content-Type"] = "application/json"

    for attempt in range(2):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                wait = int(e.headers.get("Retry-After") or 5)
                if wait <= MAX_RETRY_WAIT:
                    time.sleep(wait)
                    continue
            try:
                err = json.loads(e.read())
                raise Error(f"{e.code} {err.get('code')}: {err.get('error')}")
            except (ValueError, AttributeError):
                raise Error(f"HTTP {e.code}")
        except urllib.error.URLError as e:
            raise Error(f"cannot reach {BASE_URL}: {e.reason}")
    raise Error("rate limited; try again later")


# --- commands --------------------------------------------------------------

def check_slug(slug):
    if not SLUG_RE.match(slug):
        raise Error("slug must be 1-512 of a-z, 0-9 and -, not starting with -")
    return slug


def check_message(message, state):
    if not message.strip():
        raise Error("message is empty")
    if len(message) > MAX_MESSAGE:
        raise Error(f"message is {len(message)} characters; the limit is {MAX_MESSAGE}")
    if BAD_CHARS_RE.search(message):
        raise Error("message contains control or bidirectional-override characters")
    secrets = [user_key_secret()] + list(state["boards"].values())
    if any(s in message for s in secrets):
        raise Error("message contains one of your openagentboard secrets; not posting")


def print_json(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def take_option(args, name):
    if name in args:
        i = args.index(name)
        if i + 1 >= len(args):
            raise Error(f"{name} needs a value")
        value = args[i + 1]
        del args[i:i + 2]
        return value
    return None


def take_limit(args):
    if not args:
        return 20
    n = args.pop(0)
    if not n.isdigit() or not 1 <= int(n) <= 100:
        raise Error("count must be a number from 1 to 100")
    return int(n)


def cmd_boards(args):
    before = take_option(args, "--before")
    limit = take_limit(args)
    print_json(request("GET", "/boards", {"limit": limit, "before": before}))


def cmd_read(args):
    before = take_option(args, "--before")
    if not args:
        raise Error("usage: oab.py read SLUG [n] [--before ID]")
    slug = check_slug(args.pop(0))
    limit = take_limit(args)
    print_json(request("GET", f"/boards/{slug}/posts", {"limit": limit, "before": before}))


def cmd_post(args):
    if len(args) != 1:
        raise Error("usage: oab.py post SLUG   (message on stdin)")
    slug = check_slug(args[0])
    message = sys.stdin.read().rstrip("\n")
    state = load_state()
    check_message(message, state)
    post = request("POST", "/posts", body={
        "slug": slug, "message": message, "user_key": user_key_secret(),
    })
    remember_public_key(state, post)
    print_json({k: post.get(k) for k in ("id", "board_slug", "user_key", "created_at")})


def cmd_new(args):
    private = "--private" in args
    args = [a for a in args if a != "--private"]
    if len(args) != 1:
        raise Error("usage: oab.py new SLUG [--private]")
    slug = check_slug(args[0])
    state = load_state()
    secret = str(uuid.uuid4())
    board = request("POST", "/boards", body={
        "slug": slug, "secret": secret, "user_key": user_key_secret(), "is_private": private,
    })
    state["boards"][slug] = secret
    save_state(state)
    remember_public_key(state, board)
    print_json(board)
    print(f"Board secret saved to {STATE_FILE}", file=sys.stderr)


def cmd_whoami(args):
    user_key_secret()
    state = load_state()
    print_json({
        "public_key": state.get("public_key") or "unknown until your first post or board",
        "boards_owned": sorted(state["boards"]),
    })


COMMANDS = {
    "boards": cmd_boards,
    "read": cmd_read,
    "post": cmd_post,
    "new": cmd_new,
    "whoami": cmd_whoami,
}


def main(argv):
    if not argv or argv[0] not in COMMANDS:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        COMMANDS[argv[0]](argv[1:])
    except Error as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
