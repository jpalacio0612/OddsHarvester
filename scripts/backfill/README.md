# Pilot backfill — scripts/backfill/

Chunked backfill of OddsPortal match odds into S3 with per-match checkpointing.
Designed for running from an eu-west-1 EC2 against OddsPortal with the Quantigo
BTTS pipeline as the downstream consumer.

## Flow

1. **List match URLs for one season** (one-off):

   ```bash
   uv run python -m scripts.backfill.list_matches \
     -l england-premier-league --season 2024-2025 \
     --out data/matches-england-premier-league-2024-2025.txt
   ```

   Expect ~380 URLs for a 20-team league (EPL, LaLiga, Serie A, Ligue 1) or
   ~306 for 18-team leagues (Bundesliga, Eredivisie, Primeira Liga).

2. **Run the orchestrator**:

   ```bash
   uv run python -m scripts.backfill.orchestrator \
     --league england-premier-league \
     --season 2024-2025 \
     --matches-file data/matches-england-premier-league-2024-2025.txt \
     --bucket quantigo-odds-raw \
     --prefix oddsportal/football/england-premier-league/2024-2025/ \
     --markets 1x2,btts,over_under_2_5 \
     --concurrency 5 --chunk-size 10
   ```

   * Per-match JSONs land at
     `s3://quantigo-odds-raw/oddsportal/football/<league>/<season>/<match_hash>.json`.
   * Console + `logs/backfill-<league>-<season>.log` get one line per chunk and
     one per successful match.
   * Failed URLs append to `logs/failed-<league>-<season>.txt` for later retry.

3. **Resume after a crash**: re-run the exact same command. The orchestrator
   diffs the matches file against S3 at startup and only scrapes what is still
   missing, so no work is repeated.

4. **Retry only the failures**: point `--matches-file` at the generated
   `failed-*.txt` and re-run.

## Flags

| Flag | Default | Notes |
| --- | --- | --- |
| `-s/--sport` | `football` | Passed through to `oddsharvester`. |
| `-l/--league` | — | Required. Slug, e.g. `england-premier-league`. |
| `--season` | — | Required. `YYYY-YYYY`. |
| `--matches-file` | — | Required. One URL per line. |
| `--bucket` | — | Required. S3 bucket name. |
| `--prefix` | — | Required. S3 prefix where `<hash>.json` lands. |
| `--markets` | `1x2,btts,over_under_2_5` | Comma-separated market slugs. |
| `--concurrency` | `5` | Per `oddsharvester historic` invocation. |
| `--chunk-size` | `10` | Matches per invocation. |
| `--odds-history` | off | Adds the `--odds-history` flag; ~3x slower. |
| `--log-dir` | `logs` | Where the log + failed URL file go. |
| `--dry-run` | off | Print chunk plan (first 3 chunks) and exit. |

## Design notes

* **Why chunked instead of one big run?** `oddsharvester historic` writes its
  whole JSON array at the end of the invocation. A crash in the middle would
  lose every match scraped so far. Chunks of 10 bound the loss window.
* **Why S3 as the checkpoint store?** Every successful match is already
  persisted there, so we do not need a second source of truth. The hash in the
  match URL (`/h2h/.../#<hash>`) is the stable key.
* **Why match the failed URLs to a file?** Reproducible retries without re-
  running the cheap but non-zero `list_matches.py` step.
