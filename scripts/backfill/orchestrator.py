"""Chunked, checkpointable backfill of OddsPortal match odds into S3.

Given a pre-computed match URL list (from ``scripts/backfill/list_matches.py``),
this script:

  1. Filters out URLs whose match hash already exists under the S3 prefix.
  2. Splits the pending URLs into chunks and invokes ``oddsharvester historic``
     once per chunk with ``--match-link`` repeated.
  3. Splits the chunk JSON array into per-match records, keyed by the match
     hash, and uploads each to S3.
  4. Appends failed match URLs to ``data/failed-<league>-<season>.txt`` so the
     next run (or a targeted retry) picks them up.

Re-running the same command is the resume mechanism: successful chunks are
skipped because their hashes are already on S3.

Usage
-----
    uv run python -m scripts.backfill.orchestrator \
        --league england-premier-league \
        --season 2024-2025 \
        --matches-file data/matches-england-premier-league-2024-2025.txt \
        --bucket quantigo-odds-raw \
        --prefix oddsportal/football/england-premier-league/2024-2025/ \
        --markets 1x2,btts,over_under_2_5 \
        --concurrency 5 --chunk-size 10
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import boto3

from scripts.backfill.s3_utils import extract_match_hash, list_existing_hashes, s3_key_for

CHUNK_TIMEOUT_S = 900  # 15 min per chunk of 10 matches at concurrency=5
PROGRESS_LOG_EVERY_CHUNKS = 1  # log the progress summary after every chunk
LOGGER = logging.getLogger("backfill.orchestrator")


@dataclass
class Stats:
    total: int
    already_done: int
    pending: int
    ok: int = 0
    failed_urls: list[str] = field(default_factory=list)
    chunk_errors: int = 0
    started_at: float = field(default_factory=time.time)


def _configure_logging(log_dir: Path, league: str, season: str, verbose: bool) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"backfill-{league}-{season}.log"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_path, encoding="utf-8"),
    ]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )
    return log_path


def _read_matches(path: Path) -> list[str]:
    if not path.exists():
        raise SystemExit(f"matches-file not found: {path}")
    lines = [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]
    return lines


def _split_chunks(items: list[str], chunk_size: int) -> list[list[str]]:
    return [items[i : i + chunk_size] for i in range(0, len(items), chunk_size)]


def _build_harvester_cmd(args: argparse.Namespace, chunk: list[str], out_path: Path) -> list[str]:
    cmd: list[str] = [
        "uv",
        "run",
        "oddsharvester",
        "historic",
        "-s",
        args.sport,
        "-l",
        args.league,
        "--season",
        args.season,
        "-m",
        args.markets,
        "--full-scrape",
        "--headless",
        "-c",
        str(args.concurrency),
        "--storage",
        "local",
        "-f",
        "json",
        "-o",
        str(out_path),
    ]
    if args.odds_history:
        cmd.append("--odds-history")
    for url in chunk:
        cmd.extend(["--match-link", url])
    return cmd


def _upload_match(s3_client, bucket: str, prefix: str, record: dict) -> str:
    match_hash = extract_match_hash(record.get("match_link", "")) or "unknown"
    key = s3_key_for(prefix, match_hash)
    body = json.dumps(record, ensure_ascii=False).encode("utf-8")
    s3_client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
    return match_hash


def _fmt_duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{secs:02d}s" if hours else f"{minutes}m{secs:02d}s"


def _append_failed(path: Path, urls: list[str]) -> None:
    if not urls:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for url in urls:
            fh.write(url + "\n")


def _run_one_chunk(
    s3_client,
    args: argparse.Namespace,
    chunk: list[str],
    chunk_idx: int,
    total_chunks: int,
    stats: Stats,
) -> None:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        out_path = Path(tmp.name)
    try:
        cmd = _build_harvester_cmd(args, chunk, out_path)
        LOGGER.info("chunk %d/%d: %d matches — running oddsharvester", chunk_idx, total_chunks, len(chunk))
        t0 = time.time()
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CHUNK_TIMEOUT_S,
            check=False,
        )
        elapsed = time.time() - t0
        if result.returncode != 0:
            stats.chunk_errors += 1
            stats.failed_urls.extend(chunk)
            tail = (result.stderr or result.stdout or "").strip().splitlines()[-10:]
            LOGGER.error(
                "chunk %d/%d failed (exit=%d) after %ss — last lines:\n%s",
                chunk_idx,
                total_chunks,
                result.returncode,
                _fmt_duration(elapsed),
                "\n".join(tail),
            )
            return

        if not out_path.exists() or out_path.stat().st_size == 0:
            stats.chunk_errors += 1
            stats.failed_urls.extend(chunk)
            LOGGER.error("chunk %d/%d produced no output file", chunk_idx, total_chunks)
            return

        try:
            records = json.loads(out_path.read_text())
        except json.JSONDecodeError as err:
            stats.chunk_errors += 1
            stats.failed_urls.extend(chunk)
            LOGGER.error("chunk %d/%d: could not parse output JSON: %s", chunk_idx, total_chunks, err)
            return

        scraped_hashes: set[str] = set()
        for record in records:
            try:
                match_hash = _upload_match(s3_client, args.bucket, args.prefix, record)
                scraped_hashes.add(match_hash)
                stats.ok += 1
                LOGGER.info(
                    "chunk %d/%d OK %s (%s vs %s) %d markets",
                    chunk_idx,
                    total_chunks,
                    match_hash,
                    record.get("home_team", "?"),
                    record.get("away_team", "?"),
                    sum(1 for k in record if k.endswith("_market") and record.get(k)),
                )
            except Exception as err:
                stats.failed_urls.append(record.get("match_link", ""))
                LOGGER.exception("chunk %d/%d upload failed: %s", chunk_idx, total_chunks, err)

        missing = [u for u in chunk if (extract_match_hash(u) or "") not in scraped_hashes]
        if missing:
            LOGGER.warning(
                "chunk %d/%d: %d matches present in URL list but missing from output — tracking as failed",
                chunk_idx,
                total_chunks,
                len(missing),
            )
            stats.failed_urls.extend(missing)

        LOGGER.info(
            "chunk %d/%d done in %s — ok=%d failed=%d",
            chunk_idx,
            total_chunks,
            _fmt_duration(elapsed),
            len(scraped_hashes),
            len(missing),
        )
    except subprocess.TimeoutExpired:
        stats.chunk_errors += 1
        stats.failed_urls.extend(chunk)
        LOGGER.error("chunk %d/%d TIMEOUT after %ss — tracking all matches as failed", chunk_idx, total_chunks, CHUNK_TIMEOUT_S)
    finally:
        try:
            out_path.unlink(missing_ok=True)
        except Exception:  # pragma: no cover - best-effort cleanup
            pass


def _log_progress(stats: Stats, chunk_idx: int, total_chunks: int) -> None:
    processed = stats.ok + len(stats.failed_urls)
    elapsed = time.time() - stats.started_at
    rate = processed / elapsed if elapsed > 0 else 0
    remaining = max(stats.pending - processed, 0)
    eta_s = remaining / rate if rate > 0 else 0
    pct = (processed / stats.pending * 100) if stats.pending else 100.0
    LOGGER.info(
        "progress: chunk=%d/%d matches=%d/%d (%.1f%%) ok=%d failed=%d elapsed=%s eta=%s",
        chunk_idx,
        total_chunks,
        processed,
        stats.pending,
        pct,
        stats.ok,
        len(stats.failed_urls),
        _fmt_duration(elapsed),
        _fmt_duration(eta_s) if rate > 0 else "-",
    )


def run(args: argparse.Namespace) -> int:
    log_path = _configure_logging(Path(args.log_dir), args.league, args.season, args.verbose)
    LOGGER.info("starting backfill league=%s season=%s log=%s", args.league, args.season, log_path)
    LOGGER.info("markets=%s concurrency=%d chunk_size=%d odds_history=%s", args.markets, args.concurrency, args.chunk_size, args.odds_history)

    matches_path = Path(args.matches_file)
    all_urls = _read_matches(matches_path)
    LOGGER.info("matches file %s contains %d URLs", matches_path, len(all_urls))

    s3_client = boto3.client("s3")
    existing = list_existing_hashes(args.bucket, args.prefix, client=s3_client)
    LOGGER.info("s3://%s/%s already contains %d match JSONs", args.bucket, args.prefix, len(existing))

    pending = [url for url in all_urls if (extract_match_hash(url) or "") not in existing]
    skipped = len(all_urls) - len(pending)
    LOGGER.info("planning: %d total, %d already done, %d pending", len(all_urls), skipped, len(pending))

    if not pending:
        LOGGER.info("nothing to do — all matches already present on S3")
        return 0

    stats = Stats(total=len(all_urls), already_done=skipped, pending=len(pending))
    chunks = _split_chunks(pending, args.chunk_size)
    LOGGER.info("chunk plan: %d chunks of up to %d matches each", len(chunks), args.chunk_size)

    if args.dry_run:
        for idx, chunk in enumerate(chunks[:3], start=1):
            LOGGER.info("dry-run chunk %d preview: %s", idx, chunk[:2])
        LOGGER.info("dry-run: no scraping or uploads performed")
        return 0

    failed_file = Path(args.log_dir) / f"failed-{args.league}-{args.season}.txt"
    for idx, chunk in enumerate(chunks, start=1):
        _run_one_chunk(s3_client, args, chunk, idx, len(chunks), stats)
        if idx % PROGRESS_LOG_EVERY_CHUNKS == 0:
            _log_progress(stats, idx, len(chunks))

    _append_failed(failed_file, stats.failed_urls)
    total_elapsed = _fmt_duration(time.time() - stats.started_at)
    LOGGER.info(
        "backfill done: ok=%d failed=%d chunk_errors=%d elapsed=%s failed_file=%s",
        stats.ok,
        len(stats.failed_urls),
        stats.chunk_errors,
        total_elapsed,
        failed_file if stats.failed_urls else "(none)",
    )
    return 0 if stats.chunk_errors == 0 else 2


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-s", "--sport", default="football")
    parser.add_argument("-l", "--league", required=True)
    parser.add_argument("--season", required=True)
    parser.add_argument("--matches-file", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", required=True, help="S3 prefix ending where <hash>.json will be placed")
    parser.add_argument("--markets", default="1x2,btts,over_under_2_5")
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--chunk-size", type=int, default=10)
    parser.add_argument("--odds-history", action="store_true", help="pass --odds-history through (slower)")
    parser.add_argument("--log-dir", default="logs")
    parser.add_argument("--dry-run", action="store_true", help="print plan + first chunks and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return run(_parse_args(argv or sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
