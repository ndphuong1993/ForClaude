"""Score a completed draft from every signal available, and show the parts.

Signals, strongest evidence first:

1. Trained model (model/draft_model.json). The only component measured on a
   holdout: 53.0% accuracy against a 52.2% baseline. That is barely above
   chance, and the combined number below inherits that weakness.
2. Player hero mastery. This player's record on this hero, shrunk toward even.
3. Team hero comfort. This organisation's record on this hero.
4. Hero pro win rate on the patch.

Each is reported separately with its own implied win probability, because a
single blended number hides which signals disagree. The blend weights are
hand-set, not fitted, and are labelled as such.
"""
import json, math, os, subprocess, time, urllib.request

KEY = os.environ["STEAM_API_KEY"]
LEAGUE = int(os.environ.get("LEAGUE_ID", "19719"))
TEAM_IDS = {9572001, 7119388}
MINUTES = float(os.environ.get("MINUTES", "40"))
LOG = "live/predict.log"
VALVE = "https://api.steampowered.com/IDOTA2Match_570/GetLiveLeagueGames/v1/?key=" + KEY


def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "forclaude-predict5"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(2)


def emit(line):
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    subprocess.run(["git", "add", LOG], check=False)
    subprocess.run(["git", "commit", "-q", "-m", "predict5: " + line[:60]], check=False)
    branch = os.environ.get("GITHUB_REF_NAME", "HEAD")
    for attempt in range(5):
        if subprocess.run(["git", "push", "-q", "origin", "HEAD"]).returncode == 0:
            return
        subprocess.run(["git", "pull", "--rebase", "-q", "origin", branch], check=False)
        time.sleep(2 ** attempt)


def sigmoid(z):
    return 1.0 / (1.0 + math.exp(-max(-20.0, min(20.0, z))))


def shrink(wins, games, prior_games, base=0.5):
    return (wins + base * prior_games) / (games + prior_games)


def model_logit(model, rad_ids, dire_ids):
    idx = {h: i for i, h in enumerate(model["heroes"])}
    w, b = model["w"], model["b"]
    lin = b + sum(w[idx[h]] for h in rad_ids if h in idx) - sum(w[idx[h]] for h in dire_ids if h in idx)
    base, prior = model["base"], model["prior"]

    def sh(rec):
        return (rec[0] + base * prior) / (rec[1] + prior) - base

    syn = 0.0
    for team, sign in ((rad_ids, 1.0), (dire_ids, -1.0)):
        for i in range(5):
            for j in range(i + 1, 5):
                k = "%d_%d" % (min(team[i], team[j]), max(team[i], team[j]))
                if k in model["syn"]:
                    syn += sign * sh(model["syn"][k])
    ctr = 0.0
    for a in rad_ids:
        for d in dire_ids:
            k = "%d_%d" % (a, d)
            if k in model["ctr"]:
                ctr += sh(model["ctr"][k])
    sw, sb = model["stack_w"], model["stack_b"]
    return sb + sw[0] * lin + sw[1] * (syn / 10.0) + sw[2] * (ctr / 25.0)


def main():
    heroes = {h["id"]: h["localized_name"] for h in (get("https://api.opendota.com/api/heroes") or [])}
    hstats = {h["id"]: h for h in (get("https://api.opendota.com/api/heroStats") or [])}
    model = json.load(open("model/draft_model.json")) if os.path.exists("model/draft_model.json") else None
    team_cache, player_cache = {}, {}
    deadline = time.time() + MINUTES * 60

    while time.time() < deadline:
        feed = get(VALVE) or {}
        games = feed.get("result", {}).get("games", [])
        target = None
        for g in games:
            ids = {g.get("radiant_team", {}).get("team_id"), g.get("dire_team", {}).get("team_id")}
            if g.get("league_id") == LEAGUE or ids & TEAM_IDS:
                sb = g.get("scoreboard") or {}
                if len((sb.get("radiant", {}) or {}).get("picks", []) or []) == 5 and \
                   len((sb.get("dire", {}) or {}).get("picks", []) or []) == 5:
                    target = g
                    break
        if not target:
            time.sleep(10)
            continue

        sb = target["scoreboard"]
        rad_name = target.get("radiant_team", {}).get("team_name", "radiant")
        dire_name = target.get("dire_team", {}).get("team_name", "dire")
        rad_tid = target.get("radiant_team", {}).get("team_id")
        dire_tid = target.get("dire_team", {}).get("team_id")
        rad_ids = [p["hero_id"] for p in sb["radiant"]["picks"]]
        dire_ids = [p["hero_id"] for p in sb["dire"]["picks"]]

        emit("=== DRAFT COMPLETE match %s | %s (radiant) vs %s (dire)" % (target.get("match_id"), rad_name, dire_name))
        emit("  %s: %s" % (rad_name, ", ".join(heroes.get(h, str(h)) for h in rad_ids)))
        emit("  %s: %s" % (dire_name, ", ".join(heroes.get(h, str(h)) for h in dire_ids)))

        # 1. trained model
        if model:
            z = model_logit(model, rad_ids, dire_ids)
            t = model["test"]
            emit("  [1] trained model      %s %.1f%% - %.1f%% %s   (holdout acc %.3f vs baseline .522, auc %.3f)"
                 % (rad_name, sigmoid(z) * 100, (1 - sigmoid(z)) * 100, dire_name, t["accuracy"], t["auc"]))
        else:
            z = 0.0
            emit("  [1] trained model      unavailable")

        # 2. player mastery on the hero they are actually holding
        def player_edge(side):
            tot, lines = 0.0, []
            for p in sb[side].get("players", []) or []:
                aid, hid = p.get("account_id"), p.get("hero_id")
                if not aid or not hid:
                    continue
                if aid not in player_cache:
                    player_cache[aid] = get("https://api.opendota.com/api/players/%d/heroes" % aid) or []
                rec = next((x for x in player_cache[aid] if int(x["hero_id"]) == hid), None)
                if rec and rec["games"] > 0:
                    e = shrink(rec["win"], rec["games"], 30) - 0.5
                    tot += e
                    lines.append("%s on %s %d/%dg" % (p.get("name", aid), heroes.get(hid, hid), rec["win"], rec["games"]))
                else:
                    lines.append("%s on %s no history" % (p.get("name", aid), heroes.get(hid, hid)))
            return tot, lines

        re_, rl = player_edge("radiant")
        de_, dl = player_edge("dire")
        pz = 6.0 * (re_ - de_)
        emit("  [2] player mastery     %s %.1f%% - %.1f%% %s   (sum edge %+.3f vs %+.3f)"
             % (rad_name, sigmoid(pz) * 100, (1 - sigmoid(pz)) * 100, dire_name, re_, de_))
        emit("      %s: %s" % (rad_name, "; ".join(rl)))
        emit("      %s: %s" % (dire_name, "; ".join(dl)))

        # 3. team comfort on these heroes
        def team_edge(tid, ids):
            if tid and tid not in team_cache:
                team_cache[tid] = get("https://api.opendota.com/api/teams/%d/heroes" % tid) or []
            tot, lines = 0.0, []
            for h in ids:
                rec = next((x for x in team_cache.get(tid, []) if int(x["hero_id"]) == h), None)
                if rec and rec["games_played"] > 0:
                    e = shrink(rec["wins"], rec["games_played"], 20) - 0.5
                    tot += e
                    lines.append("%s %d/%d" % (heroes.get(h, h), rec["wins"], rec["games_played"]))
                else:
                    lines.append("%s 0g" % heroes.get(h, h))
            return tot, lines

        rte, rtl = team_edge(rad_tid, rad_ids)
        dte, dtl = team_edge(dire_tid, dire_ids)
        tz = 5.0 * (rte - dte)
        emit("  [3] team comfort       %s %.1f%% - %.1f%% %s   (sum edge %+.3f vs %+.3f)"
             % (rad_name, sigmoid(tz) * 100, (1 - sigmoid(tz)) * 100, dire_name, rte, dte))
        emit("      %s: %s" % (rad_name, ", ".join(rtl)))
        emit("      %s: %s" % (dire_name, ", ".join(dtl)))

        # 4. hero pro win rate
        def pro_rate(ids):
            vals = []
            for h in ids:
                st = hstats.get(h, {})
                if st.get("pro_pick", 0) >= 20:
                    vals.append(st["pro_win"] / st["pro_pick"])
                elif st.get("8_pick", 0):
                    vals.append(st["8_win"] / st["8_pick"])
            return sum(vals) / len(vals) if vals else 0.5

        rpr, dpr = pro_rate(rad_ids), pro_rate(dire_ids)
        hz = 14.0 * (rpr - dpr)
        emit("  [4] hero pro winrate   %s %.1f%% - %.1f%% %s   (mean %.3f vs %.3f)"
             % (rad_name, sigmoid(hz) * 100, (1 - sigmoid(hz)) * 100, dire_name, rpr, dpr))

        # blend - weights are hand-set, not fitted
        blend = 0.55 * z + 0.20 * pz + 0.15 * tz + 0.10 * hz
        p = sigmoid(blend)
        emit("  ==> COMBINED  %s %.1f%% - %.1f%% %s   [blend weights are hand-set, not validated]"
             % (rad_name, p * 100, (1 - p) * 100, dire_name))
        emit("  Honest read: only [1] was measured on held-out data, and it beats "
             "'always pick radiant' by 0.8pp. Treat anything inside 45-55% as no signal.")
        return

    emit("predict5: draft did not complete within the window")


if __name__ == "__main__":
    main()
