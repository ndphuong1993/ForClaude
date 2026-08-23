"""Poll OpenDota for a live pro match and append a win-probability line every 30s.

The win probability is a heuristic, not a trained model. It reads the gold
lead, the kill difference and the clock, and maps them through a logistic
curve whose slope grows with game time -- a 3k lead at minute 40 decides far
more than the same lead at minute 10. Building state, buybacks, item timings
and draft are all ignored, so treat the number as a rough read.
"""
import json, math, os, subprocess, time, urllib.request

TEAM_A = os.environ.get("TEAM_A", "vision")     # matched against team names, lowercase
TEAM_B = os.environ.get("TEAM_B", "spirit")
SERIES = int(os.environ.get("SERIES", "1133004"))
MINUTES = float(os.environ.get("MINUTES", "50"))
INTERVAL = float(os.environ.get("INTERVAL", "30"))   # seconds between reports, early game
LATE_MIN = float(os.environ.get("LATE_MIN", "30"))   # game minute after which reporting goes event-driven
LATE_POLL = int(os.environ.get("LATE_POLL", "25"))   # poll seconds once late; also the fight window
FIGHT_KILLS = int(os.environ.get("FIGHT_KILLS", "2"))  # deaths inside one window that count as a fight
SWING_GOLD = float(os.environ.get("SWING_GOLD", "3000"))
LOG = "live/ticker.log"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "forclaude-ticker"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def win_prob(gold_lead, kill_diff, minute):
    """P(radiant wins). gold_lead and kill_diff are radiant-minus-dire."""
    slope = min(0.45, 0.10 + 0.0083 * minute)
    logit = slope * gold_lead / 1000.0 + 0.04 * kill_diff
    logit *= min(1.0, minute / 8.0)          # the first minutes decide little
    p = 1.0 / (1.0 + math.exp(-max(-8.0, min(8.0, logit))))
    return min(0.99, max(0.01, p))


VALVE = "https://api.steampowered.com/IDOTA2Match_570/GetLiveLeagueGames/v1/?key=" + os.environ.get("STEAM_API_KEY", "")
LEAGUE = int(os.environ.get("LEAGUE_ID", "19719"))
TEAM_IDS = {9572001, 7119388}


def find_valve(games):
    """Valve's own feed, which stays current when OpenDota's live mirror stalls."""
    for g in games:
        ids = {g.get("radiant_team", {}).get("team_id"), g.get("dire_team", {}).get("team_id")}
        if g.get("league_id") == LEAGUE or ids & TEAM_IDS:
            sb = g.get("scoreboard") or {}
            if not sb:
                continue
            nw = lambda side: sum(p.get("net_worth", 0) for p in (sb.get(side, {}) or {}).get("players", []) or [])
            return {
                "match_id": g.get("match_id"),
                "rad": g.get("radiant_team", {}).get("team_name", "radiant"),
                "dire": g.get("dire_team", {}).get("team_name", "dire"),
                "seconds": sb.get("duration", 0),
                "rscore": (sb.get("radiant", {}) or {}).get("score", 0),
                "dscore": (sb.get("dire", {}) or {}).get("score", 0),
                "lead": nw("radiant") - nw("dire"),
            }
    return None


def series_score(pro):
    games = [m for m in pro if m.get("series_id") == SERIES]
    wins = {}
    for m in games:
        w = m["radiant_name"] if m["radiant_win"] else m["dire_name"]
        wins[w] = wins.get(w, 0) + 1
    return games, wins


def find_live(live, finished_ids):
    """The live feed keeps serving a game for a while after it ends, so drop
    anything that already has a result and take the latest game that started."""
    cands = []
    for m in live:
        names = (m.get("team_name_radiant") or "") + " " + (m.get("team_name_dire") or "")
        n = names.lower()
        if TEAM_A not in n or TEAM_B not in n:
            continue
        if str(m.get("match_id")) in finished_ids or m.get("deactivate_time"):
            continue
        cands.append(m)
    return max(cands, key=lambda m: m.get("activate_time", 0)) if cands else None


def emit(line):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    subprocess.run(["git", "add", LOG], check=True)
    subprocess.run(["git", "commit", "-q", "-m", "ticker: " + line[:70]], check=False)
    branch = os.environ.get("GITHUB_REF_NAME", "HEAD")
    for attempt in range(5):
        if subprocess.run(["git", "push", "-q", "origin", "HEAD"]).returncode == 0:
            return
        # Another job pushed to the branch; rebase onto it instead of giving up,
        # or the line never leaves the runner.
        subprocess.run(["git", "pull", "--rebase", "-q", "origin", branch], check=False)
        time.sleep(2 ** attempt)
    print("WARNING: could not push: " + line, flush=True)


def main():
    """Two modes. Before LATE_MIN the ticker reports on a fixed cadence. After
    it, the poll rate goes up but reporting goes quiet: only a teamfight (two
    or more deaths inside one poll window) or a large net-worth swing is worth
    interrupting for."""
    deadline = time.time() + MINUTES * 60
    last, prev_kills, prev_lead = None, None, None

    while time.time() < deadline:
        stamp = time.strftime("%H:%M:%S", time.gmtime(time.time() + 7 * 3600)) + " VN"
        late, tag = False, ""
        try:
            live, pro = get("https://api.opendota.com/api/live"), get("https://api.opendota.com/api/proMatches")
            games, wins = series_score(pro)
            finished = {str(x["match_id"]) for x in pro}
            sc = " / ".join("%s %d" % (t, w) for t, w in sorted(wins.items()))
            m = None
            try:
                m = find_valve(get(VALVE).get("result", {}).get("games", []))
            except Exception:
                m = None
            if m is None:
                od = find_live(live, finished)
                if od:
                    m = {"match_id": od["match_id"], "rad": od["team_name_radiant"], "dire": od["team_name_dire"],
                         "seconds": od["game_time"], "rscore": od["radiant_score"], "dscore": od["dire_score"],
                         "lead": od["radiant_lead"]}
            if m:
                minute = m["seconds"] / 60.0
                late = minute >= LATE_MIN
                total = m["rscore"] + m["dscore"]
                if late and prev_kills is not None:
                    dk, dg = total - prev_kills, m["lead"] - prev_lead
                    if dk >= FIGHT_KILLS:
                        tag = " | TEAMFIGHT +%d kills in %ds" % (dk, LATE_POLL)
                    elif abs(dg) >= SWING_GOLD:
                        tag = " | GOLD SWING %+.1fk in %ds" % (dg / 1000.0, LATE_POLL)
                prev_kills, prev_lead = total, m["lead"]

                rad, dire = m["rad"], m["dire"]
                p = win_prob(m["lead"], m["rscore"] - m["dscore"], minute)
                ahead = rad if m["lead"] >= 0 else dire
                line = ("%s | %02d:%02d | %s %.1f%% - %.1f%% %s | gold +%.1fk %s | kills %s %d - %d %s | series %s%s"
                        % (stamp, int(minute), int(minute % 1 * 60), rad, p * 100, (1 - p) * 100, dire,
                           abs(m["lead"]) / 1000.0, ahead,
                           rad, m["rscore"], m["dscore"], dire, sc or "0-0", tag))
            else:
                prev_kills = prev_lead = None
                line = "%s | no live game right now | series %s (%d games played)" % (stamp, sc or "0-0", len(games))
        except Exception as e:
            line = "%s | fetch failed: %s" % (stamp, e)

        # Quiet once the game is late unless something actually happened.
        if (not late) or tag:
            if line[10:] != (last or "")[10:]:
                emit(line)
                last = line
        print(line, flush=True)
        time.sleep(LATE_POLL if late else INTERVAL)


if __name__ == "__main__":
    main()
