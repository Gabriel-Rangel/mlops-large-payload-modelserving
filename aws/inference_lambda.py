"""Lambda de inferência (gatilho SQS) — chama o Model Serving só com o caminho do arquivo.

Mensagem: {"request_id", "s3_key"}. A chave S3 vira o caminho no Volume EXTERNAL, então o endpoint
recebe {"file_path": "/Volumes/.../request.json"} (~250 bytes, registrado pelas inference tables) e
lê os 29 MB sozinho, pela Files API. A decisão é gravada como response_dbx.json ao lado do
request.json, o que dispara o caminho S3 → SNS → response router.

Falhas que valem nova tentativa (429/5xx/timeout) voltam para a fila (partial batch failure; DLQ
depois de 3); as demais (4xx, ex. hash divergente) geram um response_dbx.json com status "failed".
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import boto3

import dbx_http

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
BUCKET = os.environ["BUCKET"]
ENDPOINT_NAME = os.environ["ENDPOINT_NAME"]
S3_PREFIX = os.environ.get("S3_PREFIX", "payloads/")
VOLUME_ROOT = os.environ["VOLUME_ROOT"]


def volume_path(s3_key: str) -> str:
    return f"{VOLUME_ROOT}/{s3_key[len(S3_PREFIX):]}"


def process(message: dict[str, Any]) -> dict[str, Any]:
    request_id, key = message["request_id"], message["s3_key"]
    body = json.dumps({"dataframe_records": [{"file_path": volume_path(key), "request_id": request_id}]}).encode()
    t0 = time.time()
    status, _, resp = dbx_http.http(
        "POST",
        f"{dbx_http.databricks_host()}/serving-endpoints/{ENDPOINT_NAME}/invocations",
        body,
        {"Authorization": f"Bearer {dbx_http.databricks_token()}", "Content-Type": "application/json"},
        timeout=240,
    )
    if status == 429 or status >= 500:
        raise RuntimeError(f"Model Serving HTTP {status} (nova tentativa): {resp[:300]!r}")

    result: dict[str, Any] = {
        "request_id": request_id,
        "file_path": volume_path(key),
        "endpoint_name": ENDPOINT_NAME,
        "serving_invoke_ms": round((time.time() - t0) * 1000.0, 1),
        "request_body_bytes": len(body),
    }
    if status == 200:
        pred = json.loads(resp)["predictions"][0]
        result.update(status="done", prediction=pred.get("prediction"), probability=pred.get("probability"))
    else:
        result.update(status="failed", http=status, error_message=resp[:2000].decode(errors="replace"))
    response_key = key.rsplit("/", 1)[0] + "/response_dbx.json"
    s3.put_object(Bucket=BUCKET, Key=response_key, Body=json.dumps(result).encode(), ContentType="application/json")
    logger.info(json.dumps({k: v for k, v in result.items() if k != "error_message"}))
    return result


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    failures = []
    for record in event.get("Records", []):
        try:
            process(json.loads(record["body"]))
        except Exception:  # noqa: BLE001 — o SQS tenta de novo; depois de 3, vai para a DLQ
            logger.exception("falha no request messageId=%s", record.get("messageId"))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
