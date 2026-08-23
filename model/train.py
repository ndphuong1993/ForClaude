"""Train a draft-only win model on recent pro matches.

Design, and why each piece is there:

* Linear hero term. One weight per hero, applied +1 when radiant drafted it and
  -1 when dire did. The antisymmetry is deliberate: swapping the two sides must
  flip the prediction, so the model cannot learn a "radiant is good" bias beyond
  the single intercept.
* Synergy. Empirical win rate of each same-team hero pair, shrunk toward the
  base rate by a pseudo-count, so a pair seen four times cannot swing anything.
* Counter. Same idea across teams: how often hero A's side beat hero B's side.

Pair statistics are computed on the training split only. Computing them on all
data and then scoring the holdout would leak the answer and report an accuracy
the model does not have.
"""
import json, math, os, sys, time, urllib.parse, urllib.request

DAYS = int(os.environ.get("DAYS", "365"))
LIMIT = int(os.environ.get("LIMIT", "40000"))
PRIOR = float(os.environ.get("PRIOR", "40"))     # pseudo-counts for pair shrinkage
OUT = "model/draft_model.json"

SQL = """
select m.match_id, m.radiant_win,
       array_agg(pb.hero_id) filter (where pb.is_pick and pb.team = 0) as radiant,
       array_agg(pb.hero_id) filter (where pb.is_pick and pb.team = 1) as dire
from matches m
join picks_bans pb on pb.match_id = m.match_id
where m.start_time > extract(epoch from now()) - %d
group by m.match_id, m.radiant_win
order by m.match_id desc
limit %d
""" % (DAYS * 86400, LIMIT)


def get(url, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": "forclaude-train"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def load():
    url = "https://api.opendota.com/api/explorer?" + urllib.parse.urlencode({"sql": SQL})
    data = get(url)
    if data.get("err"):
        print("explorer error: %s" % data["err"])
        sys.exit(1)
    rows = []
    for r in data.get("rows", []):
        rad, dire = r.get("radiant") or [], r.get("dire") or []
        if len(rad) == 5 and len(dire) == 5 and r.get("radiant_win") is not None:
            rows.append((rad, dire, 1 if r["radiant_win"] else 0))
    return rows


def sigmoid(z):
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def train_linear(rows, heroes, epochs=60, lr=0.30, l2=2e-4):
    idx = {h: i for i, h in enumerate(heroes)}
    w = [0.0] * len(heroes)
    b = 0.0
    for ep in range(epochs):
        for rad, dire, y in rows:
            z = b + sum(w[idx[h]] for h in rad if h in idx) - sum(w[idx[h]] for h in dire if h in idx)
            g = sigmoid(z) - y
            b -= lr * g
            for h in rad:
                if h in idx:
                    w[idx[h]] -= lr * (g + l2 * w[idx[h]])
            for h in dire:
                if h in idx:
                    w[idx[h]] -= lr * (-g + l2 * w[idx[h]])
        lr *= 0.96
    return w, b, idx


def pair_tables(rows, base):
    """Same-team pair records and cross-team matchup records."""
    syn, ctr = {}, {}
    for rad, dire, y in rows:
        for team, won in ((rad, y), (dire, 1 - y)):
            for i in range(5):
                for j in range(i + 1, 5):
                    k = (min(team[i], team[j]), max(team[i], team[j]))
                    s = syn.setdefault(k, [0, 0])
                    s[0] += won
                    s[1] += 1
        for a in rad:
            for d in dire:
                c = ctr.setdefault((a, d), [0, 0])
                c[0] += y
                c[1] += 1
    return syn, ctr


def shrunk(rec, base):
    w, n = rec
    return (w + base * PRIOR) / (n + PRIOR) - base


def features(rad, dire, w, b, idx, syn, ctr, base):
    lin = b + sum(w[idx[h]] for h in rad if h in idx) - sum(w[idx[h]] for h in dire if h in idx)
    s = 0.0
    for team, sign in ((rad, 1.0), (dire, -1.0)):
        for i in range(5):
            for j in range(i + 1, 5):
                k = (min(team[i], team[j]), max(team[i], team[j]))
                if k in syn:
                    s += sign * shrunk(syn[k], base)
    c = 0.0
    for a in rad:
        for d in dire:
            if (a, d) in ctr:
                c += shrunk(ctr[(a, d)], base)
    return [lin, s, c]


def train_stack(feats, ys, epochs=300, lr=0.05):
    w = [1.0, 0.0, 0.0]
    b = 0.0
    for ep in range(epochs):
        for x, y in zip(feats, ys):
            g = sigmoid(b + sum(wi * xi for wi, xi in zip(w, x))) - y
            b -= lr * g
            for i in range(3):
                w[i] -= lr * g * x[i]
        lr *= 0.99
    return w, b


def evaluate(name, probs, ys):
    n = len(ys)
    acc = sum(1 for p, y in zip(probs, ys) if (p >= 0.5) == (y == 1)) / n
    ll = -sum(y * math.log(max(p, 1e-9)) + (1 - y) * math.log(max(1 - p, 1e-9)) for p, y in zip(probs, ys)) / n
    pos = [p for p, y in zip(probs, ys) if y == 1]
    neg = [p for p, y in zip(probs, ys) if y == 0]
    pairs = 0
    wins = 0.0
    step = max(1, len(pos) * len(neg) // 200000)
    for i, p in enumerate(pos):
        for q in neg[::step]:
            pairs += 1
            wins += 1.0 if p > q else (0.5 if p == q else 0.0)
    auc = wins / pairs if pairs else float("nan")
    print("%-10s n=%-6d accuracy=%.4f  logloss=%.4f  auc=%.4f" % (name, n, acc, ll, auc))
    return acc, ll, auc


def main():
    t0 = time.time()
    rows = load()
    print("matches loaded: %d (%d days, limit %d)" % (len(rows), DAYS, LIMIT))
    if len(rows) < 2000:
        print("not enough data to train honestly")
        sys.exit(1)

    cut = int(len(rows) * 0.8)
    train, test = rows[cut:], rows[:cut]          # rows are newest-first, so train on older
    print("train=%d  test=%d (test is the most recent slice)" % (len(train), len(test)))

    heroes = sorted({h for r in train for h in r[0] + r[1]})
    base = sum(y for _, _, y in train) / len(train)
    print("hero count=%d  radiant base rate=%.4f" % (len(heroes), base))

    w, b, idx = train_linear(train, heroes)
    syn, ctr = pair_tables(train, base)
    print("pair tables: %d same-team pairs, %d cross matchups" % (len(syn), len(ctr)))

    ftr = [features(r[0], r[1], w, b, idx, syn, ctr, base) for r in train]
    ytr = [r[2] for r in train]
    sw, sb = train_stack(ftr, ytr)
    print("stack weights: linear=%.3f synergy=%.3f counter=%.3f bias=%.3f" % (sw[0], sw[1], sw[2], sb))

    yte = [r[2] for r in test]
    evaluate("baseline", [base] * len(test), yte)
    evaluate("linear", [sigmoid(features(r[0], r[1], w, b, idx, syn, ctr, base)[0]) for r in test], yte)
    full = [sigmoid(sb + sum(wi * xi for wi, xi in zip(sw, features(r[0], r[1], w, b, idx, syn, ctr, base)))) for r in test]
    acc, ll, auc = evaluate("full", full, yte)

    json.dump({
        "heroes": heroes, "w": w, "b": b, "stack_w": sw, "stack_b": sb,
        "base": base, "prior": PRIOR,
        "syn": {"%d_%d" % k: v for k, v in syn.items() if v[1] >= 8},
        "ctr": {"%d_%d" % k: v for k, v in ctr.items() if v[1] >= 8},
        "test": {"accuracy": acc, "logloss": ll, "auc": auc, "n": len(test)},
        "trained_on": len(train), "days": DAYS,
    }, open(OUT, "w"))
    print("wrote %s in %.0fs" % (OUT, time.time() - t0))


if __name__ == "__main__":
    main()
