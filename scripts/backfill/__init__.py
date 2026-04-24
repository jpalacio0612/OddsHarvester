"""Pilot backfill orchestrator for Quantigo-bets BTTS pipeline.

Runs OddsHarvester in chunks against a pre-computed match URL list, uploads each
scraped match to S3 under a per-match key, and resumes cleanly by diffing against
the existing S3 state. See ``scripts/backfill/README.md`` for full usage.
"""
