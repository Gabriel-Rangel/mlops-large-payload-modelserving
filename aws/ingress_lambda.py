"""Lambda de entrada (API Gateway HTTP API) — claim-check.

API Gateway (10 MB), Lambda (6 MB) e SQS (1 MiB) não carregam um request de 29 MB. Então o
payload vai para o S3 uma única vez e só o ponteiro trafega:

  POST /uploads          → {request_id, upload_url (PUT pré-assinado, 15 min), s3_key, file_path}
  PUT  <upload_url>      → o Credit Engine envia o corpo original como request.json
  POST /credit-decision  → {request_id, s3_key?}: confere o arquivo, põe o ponteiro no SQS e
                           responde 202 (mesmo contrato assíncrono de hoje)

Requests pequenos ainda podem vir inline ({var_01.., payload}); são gravados no mesmo lugar.
"""

from __future__ import annotations

import base64
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

s3 = boto3.client("s3")
sqs = boto3.client("sqs")
BUCKET = os.environ["BUCKET"]
QUEUE_URL = os.environ["QUEUE_URL"]
S3_PREFIX = os.environ.get("S3_PREFIX", "payloads/")
VOLUME_ROOT = os.environ["VOLUME_ROOT"]
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def request_key(request_id: str, day: datetime | None = None) -> str:
    return f"{S3_PREFIX}dt={(day or datetime.now(timezone.utc)):%Y-%m-%d}/{request_id}/request.json"


def volume_path(s3_key: str) -> str:
    """s3://<bucket>/payloads/<resto> é /Volumes/<catálogo>/<schema>/payloads/<resto> (Volume EXTERNAL)."""
    return f"{VOLUME_ROOT}/{s3_key[len(S3_PREFIX):]}"


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def _exists(key: str) -> bool:
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def uploads() -> dict[str, Any]:
    request_id = str(uuid.uuid4())
    key = request_key(request_id)
    url = s3.generate_presigned_url(
        "put_object", Params={"Bucket": BUCKET, "Key": key, "ContentType": "application/json"}, ExpiresIn=900
    )
    return _response(200, {"request_id": request_id, "upload_url": url, "s3_key": key, "file_path": volume_path(key)})


def credit_decision(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    raw_bytes = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode("utf-8")
    try:
        req = json.loads(raw_bytes)
    except json.JSONDecodeError:
        return _response(400, {"error": "o corpo deve ser JSON"})

    request_id = str(req.get("request_id") or uuid.uuid4())
    if not _REQUEST_ID_RE.match(request_id):
        return _response(400, {"error": "request_id deve seguir [A-Za-z0-9_-]{1,64}"})

    if isinstance(req.get("payload"), dict):
        # Request pequeno inline: grava como se tivesse vindo pelo upload.
        key = request_key(request_id)
        s3.put_object(Bucket=BUCKET, Key=key, Body=raw_bytes, ContentType="application/json")
    else:
        # Upload feito antes: procura o arquivo (hoje e ontem, por causa da virada do dia em UTC).
        now = datetime.now(timezone.utc)
        candidates = [req["s3_key"]] if req.get("s3_key") else [request_key(request_id, now - timedelta(days=d)) for d in (0, 1)]
        candidates = [k for k in candidates if k.startswith(S3_PREFIX) and f"/{request_id}/" in k]
        key = next((k for k in candidates if _exists(k)), None)
        if key is None:
            return _response(409, {"error": "request.json não encontrado: chame POST /uploads e faça o PUT antes"})

    sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps({"request_id": request_id, "s3_key": key}))
    return _response(202, {"request_id": request_id, "status": "accepted", "file_path": volume_path(key)})


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    route = event.get("routeKey")
    if route == "POST /uploads":
        return uploads()
    if route == "POST /credit-decision":
        return credit_decision(event)
    return _response(404, {"error": f"rota desconhecida: {route}"})
