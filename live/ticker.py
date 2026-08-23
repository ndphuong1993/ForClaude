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
    for attempt in range(4):
        if subprocess.run(["git", "push", "-q", "origin", "HEAD"]).returncode == 0:
            return
        time.sleep(2 ** attempt)


def main():
    deadline = time.time() + MINUTES * 60
    last = None
    while time.time() < deadline:
        stamp = time.strftime("%H:%M:%S", time.gmtime(time.time() + 7 * 3600)) + " VN"
        try:
            live, pro = get("https://api.opendota.com/api/live"), get("https://api.opendota.com/api/proMatches")
            games, wins = series_score(pro)
            finished = {str(m["match_id"]) for m in pro}
            sc = " / ".join("%s %d" % (t, w) for t, w in sorted(wins.items()))
            m = find_live(live, finished)
            if m:
                minute = m["game_time"] / 60.0
                rad, dire = m["team_name_radiant"], m["team_name_dire"]
                p = win_prob(m["radiant_lead"], m["radiant_score"] - m["dire_score"], minute)
                ahead = rad if m["radiant_lead"] >= 0 else dire
                line = ("%s | %02d:%02d | %s %.1f%% - %.1f%% %s | gold +%.1fk %s | kills %s %d - %d %s | series %s"
                        % (stamp, int(minute), int(minute % 1 * 60), rad, p * 100, (1 - p) * 100, dire,
                           abs(m["radiant_lead"]) / 1000.0, ahead,
                           rad, m["radiant_score"], m["dire_score"], dire, sc or "0-0"))
            else:
                line = "%s | no live game right now | series %s (%d games played)" % (stamp, sc or "0-0", len(games))
        except Exception as e:
            line = "%s | fetch failed: %s" % (stamp, e)
        if line[10:] != (last or "")[10:]:
            emit(line)
            last = line
        print(line, flush=True)
        time.sleep(30)


if __name__ == "__main__":
    main()
