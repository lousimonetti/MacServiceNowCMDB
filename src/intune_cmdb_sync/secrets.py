"""Indirect secret resolution.

A secret can be supplied three ways, checked in this order:

    SNOW_CLIENT_SECRET            the literal value
    SNOW_CLIENT_SECRET_FILE       a path to read it from
    SNOW_CLIENT_SECRET_PARAMETER  an AWS SSM Parameter Store name

The literal is the simplest and is what Azure Container Apps uses, because it
injects Key Vault secrets as environment variables already. `_FILE` covers
Docker/Kubernetes secret mounts. `_PARAMETER` exists for AWS Lambda, where
putting the value straight into the function's environment would expose it to
anyone holding `lambda:GetFunctionConfiguration`.
"""

from __future__ import annotations

import functools
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

FILE_SUFFIX = "_FILE"
SSM_SUFFIX = "_PARAMETER"


def resolve_secret(name: str) -> str | None:
    """Return the secret named by `name`, following `_FILE`/`_PARAMETER` indirection."""
    direct = (os.environ.get(name) or "").strip()
    if direct:
        return direct

    file_path = (os.environ.get(name + FILE_SUFFIX) or "").strip()
    if file_path:
        return _read_file(name, file_path)

    parameter = (os.environ.get(name + SSM_SUFFIX) or "").strip()
    if parameter:
        return _read_ssm_parameter(name, parameter)

    return None


def _read_file(name: str, path: str) -> str | None:
    try:
        # Trailing newlines are near-universal in mounted secret files and are
        # not part of the secret.
        return Path(path).read_text(encoding="utf-8").strip() or None
    except OSError as exc:
        log.error(
            "could not read secret from file",
            extra={"variable": name + FILE_SUFFIX, "path": path, "error": str(exc)},
        )
        return None


@functools.lru_cache(maxsize=16)
def _read_ssm_parameter(name: str, parameter: str) -> str | None:
    try:
        import boto3
    except ImportError:
        log.error(
            "an SSM parameter was configured but boto3 is not installed; "
            "install with: pip install 'intune-cmdb-sync[aws]'",
            extra={"variable": name + SSM_SUFFIX},
        )
        return None

    try:
        response = boto3.client("ssm").get_parameter(Name=parameter, WithDecryption=True)
    except Exception as exc:  # botocore raises several distinct ClientError types
        log.error(
            "could not read secret from SSM Parameter Store",
            extra={"variable": name + SSM_SUFFIX, "parameter": parameter, "error": str(exc)},
        )
        return None

    value = str(response["Parameter"]["Value"]).strip()
    return value or None
