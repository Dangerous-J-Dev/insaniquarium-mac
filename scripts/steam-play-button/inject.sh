#!/bin/bash
# Inject the native macOS launch entry for appid 3320 so its real Steam
# library Play button launches /Applications/Insaniquarium.app.
# Steam MUST be quit. Everything here is backed up and reversible (revert.sh /
# uninstall.sh).
#
# Usage: inject.sh [--phase appinfo|pending|finalize|all] [--owner <steamid64>]
#   appinfo   patch appinfo.vdf only (oslist + launch entries). setup.sh runs
#             this BEFORE asset download so Steam treats 3320 as macOS-native.
#   pending   write an update-required appmanifest (StateFlags 2). On the next
#             steam://run/3320, Steam downloads depot 3321 itself - the only
#             asset-acquisition path that works: the install wizard and
#             validate both force a fresh per-app metadata fetch that reverts
#             the appinfo edit before acting ("Invalid platform"), while the
#             run-path updater uses the already-loaded record. Verified live.
#   finalize  install-dir symlink + run.sh fallback + appmanifest (written only
#             if Steam didn't already write a fully-installed one itself).
#   all       appinfo + finalize (default; the original single-shot behavior).
# --owner, or INSANIQ_STEAMID=<steamid64> in the environment, names the Steam
# account for the appmanifest instead of reading it from Steam's files.
set -euo pipefail
cd "$(dirname "$0")"

STEAM="$HOME/Library/Application Support/Steam"
APPINFO="$STEAM/appcache/appinfo.vdf"
GID="4977529675831131285"       # depot 3321 public manifest (up-to-date => no download)
BUILDID="250752"
INSTALLDIR="Insaniquarium Deluxe"
source ./app-launch.sh

PHASE=all
OWNER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --phase) PHASE="${2:?}"; shift 2;;
    --owner) OWNER="${2:?}"; shift 2;;
    *) echo "usage: inject.sh [--phase appinfo|pending|finalize|all] [--owner <steamid64>]"; exit 2;;
  esac
done
case "$PHASE" in appinfo|pending|finalize|all) ;; *) echo "error: bad --phase '$PHASE'"; exit 2;; esac

if pgrep -x steam_osx >/dev/null; then
  echo "error: Steam is running - quit it first (osascript -e 'quit app \"Steam\"')"; exit 1
fi
[ -f "$APPINFO" ] || { echo "error: $APPINFO not found - is Steam installed and signed in?"; exit 1; }

# SteamID64 of the Steam account the game files download under, for the
# appmanifest's LastOwner field. A Mac can have several accounts signed in, and
# current Steam clients no longer flag the most recent one in loginusers.vdf,
# so read the account Steam signs in to by itself first. Prints the ID on
# stdout and "Steam account: <name> (<why>)" on stderr, or nothing if Steam's
# files don't say. Exits 2 if --owner/INSANIQ_STEAMID isn't a Steam ID.
detect_steamid() {
  local override="$OWNER" source="--owner"
  if [ -z "$override" ]; then override="${INSANIQ_STEAMID:-}"; source="INSANIQ_STEAMID"; fi
  python3 - "$STEAM" "$override" "$source" <<'EOF'
import os
import re
import sys

steam, override, source = sys.argv[1:4]
BASE = 76561197960265728  # a player's SteamID64 is BASE + their account id

def say(msg):
    # Bytes, so an unusual locale can't turn a persona name into a crash.
    sys.stderr.buffer.write((msg + "\n").encode("utf-8", "replace"))
    sys.stderr.flush()

def pick(sid, why, name=""):
    print(sid, flush=True)
    say(f"Steam account: {name or sid} ({why})")
    sys.exit(0)

def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""

def vdf(text):
    # Steam's text VDF: quoted strings and braces. Keys are lower-cased: VDF
    # keys are case-insensitive, and Steam has changed their case before.
    root = cur = {}
    stack, key = [], None
    for m in re.finditer(r'"((?:[^"\\]|\\.)*)"|([{}])', text):
        tok, brace = m.group(1), m.group(2)
        if brace == "{":
            child = {}
            if key is not None:
                cur[key.lower()] = child
            stack.append(cur)
            cur, key = child, None
        elif brace == "}":
            cur, key = (stack.pop() if stack else root), None
        elif key is None:
            key = tok
        else:
            cur[key.lower()], key = re.sub(r'\\(["\\])', r"\1", tok), None
    return root

def get(d, *path):
    for k in path:
        d = d.get(k) if isinstance(d, dict) else None
    return d

def text(d, k):
    v = d.get(k)
    return v if isinstance(v, str) else ""

def digits(s):
    return re.fullmatch(r"[0-9]+", s) is not None

def unique_max(items, key):
    # The item with the highest non-zero key; None on a tie or if all are 0.
    ranked = sorted(items, key=key, reverse=True)
    if ranked and key(ranked[0]) and (len(ranked) == 1 or key(ranked[0]) != key(ranked[1])):
        return ranked[0]
    return None

if override:
    if len(override) == 17 and digits(override) and 0 < int(override) - BASE <= 0xFFFFFFFF:
        pick(override, "set by " + source)
    say(f"error: {source}={override} is not a Steam ID. In Steam, click your account")
    say("       name (top right) > Account details: your Steam ID is the")
    say("       17-digit number shown there.")
    sys.exit(2)

# The accounts that have signed in to Steam on this Mac (and were remembered).
users = []
listed = get(vdf(read(os.path.join(steam, "config", "loginusers.vdf"))), "users")
for sid, u in (listed.items() if isinstance(listed, dict) else []):
    if isinstance(u, dict) and len(sid) == 17 and digits(sid):
        ts = text(u, "timestamp")
        users.append({"sid": sid, "account": text(u, "accountname").lower(),
                      "name": text(u, "personaname"),
                      "recent": text(u, "mostrecent") == "1",
                      "ts": int(ts) if digits(ts) else 0})

# 1. The account Steam signs in to by itself. setup.sh starts Steam for the
#    download, so this is the account the download runs under.
auto = get(vdf(read(os.path.join(steam, "registry.vdf"))),
           "registry", "hkcu", "software", "valve", "steam", "autologinuser")
if isinstance(auto, str) and auto:
    for u in users:
        if u["account"] == auto.lower():
            pick(u["sid"], "Steam signs in to it automatically", u["name"])
# 2. Older Steam clients flag the account that signed in last.
recent = [u for u in users if u["recent"]]
if len(recent) == 1:
    pick(recent[0]["sid"], "it signed in to Steam last", recent[0]["name"])
# 3. Only one account has signed in.
if len(users) == 1:
    pick(users[0]["sid"], "the only account signed in to Steam on this Mac", users[0]["name"])
# 4. Current clients write no such flag; the newest sign-in time tells.
last = unique_max(users, lambda x: x["ts"])
if last:
    pick(last["sid"], "it signed in to Steam last", last["name"])
# 5. No account list: each account that has used Steam has a userdata folder.
ud = os.path.join(steam, "userdata")
try:
    ids = [d for d in os.listdir(ud) if digits(d) and 0 < int(d) <= 0xFFFFFFFF]
except OSError:
    ids = []

def used(d):
    try:
        return os.path.getmtime(os.path.join(ud, d, "config", "localconfig.vdf"))
    except OSError:
        return 0

if len(ids) == 1:
    pick(str(int(ids[0]) + BASE), "the only account that has used Steam on this Mac")
newest = unique_max(ids, used)
if newest:
    pick(str(int(newest) + BASE), "it used Steam last")
EOF
}

# Sets STEAMID, or stops and says what to do.
steamid_or_die() {
  STEAMID="$(detect_steamid)" || exit 1
  [ -n "$STEAMID" ] && return
  {
    echo "error: couldn't tell which Steam account to use. Open Steam, sign in"
    echo "       with \"Remember me\" ticked, quit Steam, then"
    echo "       ${INSANIQ_RERUN:-run ./setup.sh again}."
    echo "       Still stuck? In Steam, click your account name (top right) >"
    echo "       Account details, and copy your Steam ID (17 digits). Then"
    if [ -n "${INSANIQ_RERUN:-}" ]; then
      echo "       paste the install command again, ending it with"
      echo "         | INSANIQ_STEAMID=<your Steam ID> bash"
      echo "       instead of | bash."
    else
      echo "       run: INSANIQ_STEAMID=<your Steam ID> ./setup.sh"
    fi
  } >&2
  exit 1
}

do_appinfo() {
  local bak="$APPINFO.bak.$(date +%s)"
  cp "$APPINFO" "$bak"
  [ -f "$APPINFO.orig" ] || cp "$APPINFO" "$APPINFO.orig"
  echo "backed up appinfo.vdf -> $bak"
  python3 edit_appinfo.py inject "$APPINFO"
}

do_pending() {
  local acf="$STEAM/steamapps/appmanifest_3320.acf"
  if [ -f "$acf" ] && grep -q '"StateFlags"[[:space:]]*"4"' "$acf"; then
    echo "appmanifest already fully installed; leaving it alone"
    return
  fi
  steamid_or_die
  cat > "$acf" <<EOF
"AppState"
{
	"appid"		"3320"
	"Universe"		"1"
	"name"		"Insaniquarium! Deluxe"
	"StateFlags"		"2"
	"installdir"		"$INSTALLDIR"
	"buildid"		"0"
	"LastOwner"		"$STEAMID"
	"AutoUpdateBehavior"		"0"
	"AllowOtherDownloadsWhileRunning"		"0"
	"ScheduledAutoUpdate"		"0"
}
EOF
  echo "wrote pending appmanifest (StateFlags 2) - steam://run/3320 will download the depot"
}

do_finalize() {
  # Let Steam launch the .app bundle. run.sh remains an optional fallback.
  ensure_app_launch_link
  cp run.sh "$STEAM/steamapps/common/$INSTALLDIR/run.sh"
  chmod +x "$STEAM/steamapps/common/$INSTALLDIR/run.sh"
  echo "installed launch symlink + run.sh fallback into steamapps/common/$INSTALLDIR"

  local acf="$STEAM/steamapps/appmanifest_3320.acf"
  # If Steam wrote a fully-installed manifest itself (the user clicked Install
  # after the appinfo phase), keep it verbatim - Steam's own LastOwner is the
  # ground truth, which matters on Family Sharing machines.
  if [ -f "$acf" ] && grep -q '"StateFlags"[[:space:]]*"4"' "$acf" \
                   && grep -q "\"installdir\"[[:space:]]*\"$INSTALLDIR\"" "$acf"; then
    echo "keeping existing appmanifest_3320.acf (fully installed)"
    return
  fi

  steamid_or_die
  # Fully-installed manifest (StateFlags 4). Correct gid => Steam sees it as
  # up to date and does not queue a download. No platform_override.
  cat > "$acf" <<EOF
"AppState"
{
	"appid"		"3320"
	"Universe"		"1"
	"name"		"Insaniquarium! Deluxe"
	"StateFlags"		"4"
	"installdir"		"$INSTALLDIR"
	"LastUpdated"		"$(date +%s)"
	"SizeOnDisk"		"19098646"
	"buildid"		"$BUILDID"
	"LastOwner"		"$STEAMID"
	"AutoUpdateBehavior"		"1"
	"AllowOtherDownloadsWhileRunning"		"0"
	"ScheduledAutoUpdate"		"0"
	"InstalledDepots"
	{
		"3321"
		{
			"manifest"		"$GID"
			"size"		"19098646"
		}
	}
}
EOF
  echo "wrote appmanifest_3320.acf (StateFlags 4, LastOwner $STEAMID)"
}

case "$PHASE" in
  appinfo)  do_appinfo;;
  pending)  do_pending;;
  finalize) do_finalize;;
  all)      do_appinfo; do_finalize;;
esac

if [ "$PHASE" = all ]; then
  echo
  echo "done. Start Steam, check appid 3320 shows Play, click it."
fi
