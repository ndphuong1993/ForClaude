# ForClaude

Workflows that fetch esports match data from a GitHub Actions runner, because
the assistant's own session container has no outbound access to these sites.

| Workflow | What it does |
| --- | --- |
| `match-status.yml` | Summarizes an OpenDota series: per-game results, series score, any live game. Input: `series_id`. |
| `fetch-match.yml` | Fetches an arbitrary URL (curl + optional headless-browser render) and prints the extracted text. Input: `url`. |
| `alt-sources.yml` | Probes candidate data sources to see which ones answer a runner. |

All three are manual (`workflow_dispatch`).

## Findings

- `cyberscore.live` sits behind Cloudflare and returns **403** to a runner IP,
  for plain curl and for real headless Chromium alike. Not usable from CI.
- `api.opendota.com` works with no key: `/api/proMatches` for finished pro games
  (grouped by `series_id`) and `/api/live` for games in progress (~2 min delay).
- `liquipedia.net` requires gzip request encoding (`Accept-Encoding: gzip`) and a
  descriptive User-Agent per its API terms.
