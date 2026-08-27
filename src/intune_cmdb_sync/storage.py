"""Tiny storage abstraction for the state file.

Two backends, chosen by the shape of `STATE_PATH`:

  /mnt/state/state.json   local filesystem (Azure Files mount, container volume, laptop)
  s3://bucket/key.json    S3, for AWS Lambda

The S3 backend exists for one specific reason: a Lambda placed in a VPC to reach
EFS also needs a NAT gateway to reach Microsoft Graph, and a NAT gateway costs
more per month than everything else in this design combined. Keeping the Lambda
out of a VPC and putting state in S3 keeps the AWS bill at effectively zero.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

log = logging.getLogger(__name__)

S3_SCHEME = "s3://"


class StateStore(Protocol):
    location: str

    def read(self) -> str | None: ...
    def write(self, payload: str) -> None: ...


class LocalStateStore:
    def __init__(self, path: str) -> None:
        self.location = path
        self._path = Path(path)

    def read(self) -> str | None:
        if not self._path.is_file():
            return None
        try:
            return self._path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("could not read state file", extra={"path": self.location,
                                                            "error": str(exc)})
            return None

    def write(self, payload: str) -> None:
        """Persist the state file, raising on failure.

        A silent failure here is worse than a loud one: the run looks clean, but
        the next one starts from empty state and quietly stops retiring.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace so a crash mid-write cannot leave a truncated file.
        handle, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=self._path.name, suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_name, self._path)
        except OSError:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise


class S3StateStore:
    def __init__(self, url: str) -> None:
        self.location = url
        parsed = urlparse(url)
        self._bucket = parsed.netloc
        self._key = parsed.path.lstrip("/")
        if not self._bucket or not self._key:
            raise ValueError(f"STATE_PATH must look like s3://bucket/key.json (got {url!r})")

    def _client(self):  # type: ignore[no-untyped-def]
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - optional extra
            raise RuntimeError(
                "STATE_PATH uses s3:// but boto3 is not installed. "
                "Install with: pip install 'intune-cmdb-sync[aws]'"
            ) from exc
        return boto3.client("s3")

    def read(self) -> str | None:
        client = self._client()
        try:
            response = client.get_object(Bucket=self._bucket, Key=self._key)
        except Exception as exc:  # botocore raises ClientError subclasses
            if "NoSuchKey" in type(exc).__name__ or "NoSuchKey" in str(exc):
                return None
            log.warning("could not read S3 state", extra={"path": self.location,
                                                          "error": str(exc)})
            return None
        return str(response["Body"].read().decode("utf-8"))

    def write(self, payload: str) -> None:
        """Persist the state object, raising on failure. See LocalStateStore.write."""
        self._client().put_object(
            Bucket=self._bucket,
            Key=self._key,
            Body=payload.encode("utf-8"),
            ContentType="application/json",
        )


def build_state_store(location: str | None) -> StateStore | None:
    if not location:
        return None
    if location.startswith(S3_SCHEME):
        return S3StateStore(location)
    return LocalStateStore(location)
