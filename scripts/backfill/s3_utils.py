"""Small helpers for S3-based checkpointing of the pilot backfill.

Two concerns live here:
- ``extract_match_hash``: parse the match-specific hash out of an OddsPortal
  ``/h2h/.../#<hash>[:market;...]`` URL. This becomes the stable filename key.
- ``list_existing_hashes``: page through ``s3://<bucket>/<prefix>`` to build the
  set of hashes already scraped so the orchestrator can skip them.
"""

from __future__ import annotations

import logging
import re

import boto3

_HASH_RE = re.compile(r"/h2h/[^/]+/[^/]+/?#(?P<hash>[^:;]+)")


def extract_match_hash(url: str) -> str | None:
    """Return the match hash encoded in an OddsPortal match URL, or ``None``.

    Handles both bare ``#<hash>`` and suffixed ``#<hash>:MARKET;2`` forms.
    """
    match = _HASH_RE.search(url)
    return match.group("hash") if match else None


def list_existing_hashes(bucket: str, prefix: str, client: object | None = None) -> set[str]:
    """List ``.json`` keys under ``s3://bucket/prefix`` and return the stems.

    Uses ``list_objects_v2`` with continuation tokens so the full prefix is walked
    even past the 1000-key page. Keys that do not end in ``.json`` are ignored.
    """
    logger = logging.getLogger(__name__)
    s3 = client or boto3.client("s3")
    hashes: set[str] = set()
    continuation: str | None = None
    while True:
        kwargs: dict = {"Bucket": bucket, "Prefix": prefix}
        if continuation:
            kwargs["ContinuationToken"] = continuation
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".json"):
                continue
            stem = key.rsplit("/", 1)[-1].removesuffix(".json")
            hashes.add(stem)
        if not resp.get("IsTruncated"):
            break
        continuation = resp.get("NextContinuationToken")
    logger.debug("Listed %d existing hashes under s3://%s/%s", len(hashes), bucket, prefix)
    return hashes


def s3_key_for(prefix: str, match_hash: str) -> str:
    """Build ``<prefix>/<hash>.json``, collapsing any duplicated slashes."""
    cleaned_prefix = prefix.rstrip("/")
    return f"{cleaned_prefix}/{match_hash}.json"
