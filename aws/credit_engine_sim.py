"""Lambda que simula o Credit Engine no fluxo com claim-check.

Gera um request sintético (15 variáveis + JSON do birô) e mede o tempo até a decisão chegar pelo
caminho de produção: response_dbx.json no S3 → SNS → fila inbox (no lugar do response router).

Eventos:
  {"mode": "claim_check", "payload_mb": 29}   upload no S3 + POST pequeno (a proposta)
  {"mode": "inline", "payload_mb": 29}        o corpo inteiro no API Gateway (fluxo de hoje; espera 413)
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from typing import Any

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

import dbx_http
from payloads import make_request_body

s3 = boto3.client("s3")
sqs = boto3.client("sqs")
API_URL = os.environ.get("API_URL", "").rstrip("/")
BUCKET = os.environ["BUCKET"]
INBOX_URL = os.environ["INBOX_URL"]


def _ms(t0: float) -> float:
    return round((time.time() - t0) * 1000.0, 1)


def signed_post(path: str, body: bytes, timeout: float = 30) -> tuple[int, bytes]:
    """POST no API Gateway com autenticação IAM (SigV4), como um Credit Engine hospedado na AWS faria."""
    url = f"{API_URL}{path}"
    creds = boto3.Session().get_credentials().get_frozen_credentials()
    req = AWSRequest(method="POST", url=url, data=body, headers={"Content-Type": "application/json"})
    SigV4Auth(creds, "execute-api", os.environ["AWS_REGION"]).add_auth(req)
    status, _, resp = dbx_http.http("POST", url, body, dict(req.headers.items()), timeout=timeout)
    return status, resp


def s3_keys(message_body: str) -> list[str]:
    """Chaves S3 de uma mensagem da inbox (evento S3 cru, ou envelope SNS se a entrega não for raw)."""
    event = json.loads(message_body)
    if "Message" in event:
        event = json.loads(event["Message"])
    return [urllib.parse.unquote_plus(r["s3"]["object"]["key"]) for r in event.get("Records", [])]


def wait_for_decision(request_id: str, deadline: float) -> dict[str, Any] | None:
    """Long polling na inbox. A fila é só de teste: mensagens de outros requests também são apagadas."""
    while time.time() < deadline:
        wait = max(1, min(20, int(deadline - time.time())))
        for msg in sqs.receive_message(QueueUrl=INBOX_URL, MaxNumberOfMessages=10, WaitTimeSeconds=wait).get("Messages", []):
            sqs.delete_message(QueueUrl=INBOX_URL, ReceiptHandle=msg["ReceiptHandle"])
            for key in s3_keys(msg["Body"]):
                if f"/{request_id}/" in key:
                    return json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    return None


def claim_check(body: bytes, timeout_s: float) -> dict[str, Any]:
    t0 = time.time()
    status, resp = signed_post("/uploads", b"{}")
    if status != 200:
        return {"ok": False, "step": "uploads", "http": status, "body": resp[:300].decode(errors="replace")}
    ticket = json.loads(resp)
    t_up = time.time()
    status, _, resp = dbx_http.http("PUT", ticket["upload_url"], body, {"Content-Type": "application/json"}, timeout=timeout_s)
    upload_ms = _ms(t_up)
    if status != 200:
        return {"ok": False, "step": "s3_put", "http": status, "body": resp[:300].decode(errors="replace")}
    t_post = time.time()
    status, resp = signed_post("/credit-decision", json.dumps({"request_id": ticket["request_id"], "s3_key": ticket["s3_key"]}).encode())
    accept_ms = _ms(t_post)
    if status != 202:
        return {"ok": False, "step": "credit_decision", "http": status, "body": resp[:300].decode(errors="replace")}
    decision = wait_for_decision(ticket["request_id"], t0 + timeout_s)
    return {
        "ok": bool(decision and decision.get("status") == "done"),
        "request_id": ticket["request_id"],
        "upload_ms": upload_ms,
        "time_to_202_ms": accept_ms,
        "e2e_ms": _ms(t0),  # do início do upload até a decisão chegar na inbox
        "serving_invoke_ms": (decision or {}).get("serving_invoke_ms"),
        "prediction": (decision or {}).get("prediction"),
    }


def inline(body: bytes) -> dict[str, Any]:
    t0 = time.time()
    status, resp = signed_post("/credit-decision", body, timeout=120)
    return {"ok": status == 413, "http": status, "elapsed_ms": _ms(t0), "body": resp[:200].decode(errors="replace")}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    mode = event.get("mode", "claim_check")
    size_mb = float(event.get("payload_mb", 29))
    t_gen = time.time()
    body = make_request_body(size_mb, seed=int(size_mb * 100) + 3)
    result = claim_check(body, float(event.get("timeout_s", 150))) if mode == "claim_check" else inline(body)
    result.update(mode=mode, payload_mb=size_mb, body_bytes=len(body), generate_ms=_ms(t_gen))
    print(json.dumps(result))
    return result
