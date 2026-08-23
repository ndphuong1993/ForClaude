"""Wait for the draft to complete, then score both sides from pro hero win rates.

This is a weak model and says so in its own output. It knows one thing: how
each drafted hero performs in pro games on the current patch, per OpenDota's
heroStats. It does not know lane assignments, synergy, counters, player hero
mastery, or which team is on the better side of the series.
"""
import json, os, subprocess, time, urllib.request

KEY = os.environ["STEAM_API_KEY"]
LEAGUE = int(os.environ.get("LEAGUE_ID", "19719"))
TEAM_IDS = {9572001, 7119388}
MINUTES = float(os.environ.get("MINUTES", "25"))
LOG = "live/predict.log"
VALVE = "https://api.steampowered.com/IDOTA2Match_570/GetLiveLeagueGames/v1/?key=" + KEY


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "forclaude-predict"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def emit(line):
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    subprocess.run(["git", "add", LOG], check=False)
    subprocess.run(["git", "commit", "-q", "-m", "predict: " + line[:60]], check=False)
    branch = os.environ.get("GITHUB_REF_NAME", "HEAD")
    for attempt in range(5):
        if subprocess.run(["git", "push", "-q", "origin", "HEAD"]).returncode == 0:
            return
        subprocess.run(["git", "pull", "--rebase", "-q", "origin", branch], check=False)
        time.sleep(2 ** attempt)


def hero_rate(st):
    """Pro win rate, falling back to high-bracket pubs when the pro sample is thin."""
    if st.get("pro_pick", 0) >= 20:
        return st["pro_win"] / st["pro_pick"], st["pro_pick"], "pro"
    pick = st.get("8_pick", 0) or st.get("7_pick", 0) or 1
    win = st.get("8_win", 0) or st.get("7_win", 0)
    return win / pick, pick, "pub"


def main():
    stats = {h["id"]: h for h in get("https://api.opendota.com/api/heroStats")}
    deadline = time.time() + MINUTES * 60
    while time.time() < deadline:
        try:
            games = get(VALVE).get("result", {}).get("games", [])
        except Exception:
            time.sleep(10)
            continue
        ours = [g for g in games
                if g.get("league_id") == LEAGUE
                or {g.get("radiant_team", {}).get("team_id"), g.get("dire_team", {}).get("team_id")} & TEAM_IDS]
        for g in ours:
            sb = g.get("scoreboard") or {}
            rp = [p["hero_id"] for p in (sb.get("radiant", {}) or {}).get("picks", []) or []]
            dp = [p["hero_id"] for p in (sb.get("dire", {}) or {}).get("picks", []) or []]
            if len(rp) < 5 or len(dp) < 5:
                continue

            rad = g.get("radiant_team", {}).get("team_name", "radiant")
            dire = g.get("dire_team", {}).get("team_name", "dire")
            rows = {}
            for side, ids in (("r", rp), ("d", dp)):
                rows[side] = [(stats[i]["localized_name"],) + hero_rate(stats[i]) for i in ids if i in stats]
            ravg = sum(x[1] for x in rows["r"]) / len(rows["r"])
            davg = sum(x[1] for x in rows["d"]) / len(rows["d"])
            # 5 heroes each: a 1pp mean edge is small, so keep the slope modest.
            p = 1 / (1 + pow(2.718281828, -(14.0 * (ravg - davg))))
            p = min(0.85, max(0.15, p))

            emit("DRAFT COMPLETE match %s | %s %.1f%% - %.1f%% %s   [hero-winrate only - weak model]"
                 % (g.get("match_id"), rad, p * 100, (1 - p) * 100, dire))
            emit("  %s: %s" % (rad, ", ".join("%s %.0f%%(%s)" % (n, r * 100, src) for n, r, _, src in rows["r"])))
            emit("  %s: %s" % (dire, ", ".join("%s %.0f%%(%s)" % (n, r * 100, src) for n, r, _, src in rows["d"])))
            emit("  mean hero winrate: %s %.1f%% vs %s %.1f%%" % (rad, ravg * 100, dire, davg * 100))
            return
        time.sleep(10)
    emit("draft did not complete within the watch window")


if __name__ == "__main__":
    main()
