"""Watch Valve's live league feed and report each ban/pick as it happens.

GetLiveLeagueGames carries scoreboard.radiant/dire .picks[] and .bans[] while
the teams are still drafting, which no public no-key source exposes. Hero ids
are mapped to names through OpenDota's hero list.
"""
import json, os, subprocess, time, urllib.request

KEY = os.environ["STEAM_API_KEY"]
TEAM_A = os.environ.get("TEAM_A", "vision")
TEAM_B = os.environ.get("TEAM_B", "spirit")
MINUTES = float(os.environ.get("MINUTES", "40"))
LEAGUE = int(os.environ.get("LEAGUE_ID", "19719"))       # The International 2026
TEAM_IDS = {9572001, 7119388}                            # TEAM VISION, Team Spirit
LOG = "live/draft.log"
VALVE = "https://api.steampowered.com/IDOTA2Match_570/GetLiveLeagueGames/v1/?key=" + KEY


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "forclaude-draft"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def emit(line):
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    subprocess.run(["git", "add", LOG], check=False)
    subprocess.run(["git", "commit", "-q", "-m", "draft: " + line[:70]], check=False)
    for attempt in range(4):
        if subprocess.run(["git", "push", "-q", "origin", "HEAD"]).returncode == 0:
            return
        time.sleep(2 ** attempt)


def main():
    heroes = {h["id"]: h["localized_name"] for h in get("https://api.opendota.com/api/heroes")}
    deadline = time.time() + MINUTES * 60
    seen, announced, last_debug = set(), set(), 0

    while time.time() < deadline:
        stamp = time.strftime("%H:%M:%S", time.gmtime(time.time() + 7 * 3600)) + " VN"
        try:
            games = get(VALVE).get("result", {}).get("games", [])
        except Exception as e:
            time.sleep(15)
            continue

        def is_ours(g):
            """Valve omits team_name on some entries, so never rely on names alone."""
            names = (str(g.get("radiant_team", {}).get("team_name", "")) +
                     str(g.get("dire_team", {}).get("team_name", ""))).lower()
            if TEAM_A in names and TEAM_B in names:
                return True
            ids = {g.get("radiant_team", {}).get("team_id"), g.get("dire_team", {}).get("team_id")}
            if ids & TEAM_IDS:
                return True
            return g.get("league_id") == LEAGUE

        ours = [g for g in games if is_ours(g)]

        if not ours:
            # Say what the feed *does* carry, so a name mismatch is visible rather than silent.
            if time.time() - last_debug > 120:
                names = ["%s vs %s [league %s, ids %s/%s]" % (
                    g.get("radiant_team", {}).get("team_name", "?"),
                    g.get("dire_team", {}).get("team_name", "?"),
                    g.get("league_id"),
                    g.get("radiant_team", {}).get("team_id"),
                    g.get("dire_team", {}).get("team_id")) for g in games]
                emit("%s | waiting | %d live league games: %s" % (stamp, len(games), "; ".join(names[:6]) or "none"))
                last_debug = time.time()
            time.sleep(15)
            continue

        for g in ours:
            mid = g.get("match_id")
            sb = g.get("scoreboard") or {}
            if mid not in announced:
                emit("%s | DRAFT STARTED | %s (radiant) vs %s (dire) | match %s"
                     % (stamp, g.get("radiant_team", {}).get("team_name", "?"),
                        g.get("dire_team", {}).get("team_name", "?"), mid))
                announced.add(mid)
            for side in ("radiant", "dire"):
                team = g.get(side + "_team", {}).get("team_name", side)
                for kind in ("bans", "picks"):
                    for e in (sb.get(side, {}) or {}).get(kind, []) or []:
                        hid = e.get("hero_id")
                        k = (mid, side, kind, hid)
                        if hid and k not in seen:
                            seen.add(k)
                            emit("%s | %s %-4s %s | match %s"
                                 % (stamp, team, "BAN" if kind == "bans" else "PICK",
                                    heroes.get(hid, "hero " + str(hid)), mid))
            if sb.get("duration", 0) > 0 and (mid, "started") not in seen:
                seen.add((mid, "started"))
                emit("%s | GAME STARTED (draft complete) | match %s" % (stamp, mid))
        time.sleep(15)


if __name__ == "__main__":
    main()
