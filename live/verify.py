"""Compare the draft captured live against Valve's official picks_bans.

The live watcher reads scoreboard.picks/bans while the teams are drafting;
OpenDota exposes the authoritative picks_bans once the match is parsed. If the
live read is trustworthy, every hero in the official draft appears in the
captured log, on the correct side and with the correct pick/ban kind.
"""
import json, os, re, sys, urllib.request

MATCH = sys.argv[1]
LOG = "live/draft.log"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "forclaude-verify"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


heroes = {h["id"]: h["localized_name"] for h in get("https://api.opendota.com/api/heroes")}
m = get("https://api.opendota.com/api/matches/" + MATCH)
if not m.get("picks_bans"):
    print("match %s has no picks_bans yet - not parsed, or still live" % MATCH)
    sys.exit(0)

# team 0 = radiant, team 1 = dire in OpenDota's picks_bans
radiant, dire = m.get("radiant_name") or "radiant", m.get("dire_name") or "dire"
official = {}
for e in m["picks_bans"]:
    official[heroes.get(e["hero_id"], str(e["hero_id"]))] = (
        radiant if e["team"] == 0 else dire, "PICK" if e["is_pick"] else "BAN")

captured = {}
for line in open(LOG, encoding="utf-8") if os.path.exists(LOG) else []:
    mt = re.search(r"\| (.+?) (BAN|PICK)\s+(.+?) \| match " + MATCH, line)
    if mt:
        captured[mt.group(3).strip()] = (mt.group(1).strip(), mt.group(2))

hit = [h for h in official if h in captured]
miss = [h for h in official if h not in captured]
extra = [h for h in captured if h not in official]
wrong = [(h, official[h], captured[h]) for h in hit
         if official[h][1] != captured[h][1] or official[h][0].lower() not in captured[h][0].lower()]

print("official draft entries : %d" % len(official))
print("captured live          : %d" % len(captured))
print("matched                : %d" % len(hit))
print("missed (never seen)    : %s" % (", ".join(miss) or "none"))
print("extra (not in official): %s" % (", ".join(extra) or "none"))
if wrong:
    for h, o, c in wrong:
        print("MISLABELLED %s: official %s %s, captured %s %s" % (h, o[0], o[1], c[0], c[1]))
else:
    print("side/kind labels       : all correct" if hit else "side/kind labels       : nothing to compare")
print("\nVERDICT: %s" % ("accurate" if hit and not miss and not wrong
                         else "incomplete or wrong - do not trust the live read"))
