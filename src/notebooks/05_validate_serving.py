# Databricks notebook source
# MAGIC %md
# MAGIC # 05 — Validação do endpoint com `{"file_path": "/Volumes/..."}` (até 32 MB)
# MAGIC
# MAGIC Simula a Lambda de inferência: o `request.json` (15 variáveis + JSON do birô) já está no
# MAGIC Volume e o POST ao endpoint leva **só o caminho**. Autenticação: OAuth do `credit-engine-client`.
# MAGIC
# MAGIC | Check | O que prova |
# MAGIC |---|---|
# MAGIC | `warmup` | o endpoint responde (com scale-to-zero, o primeiro request pode levar minutos) |
# MAGIC | `body_lt_1mib` | o corpo cabe nas inference tables (1 MiB) |
# MAGIC | `http_200_prediction` | o modelo leu o arquivo pela Files API e pontuou |
# MAGIC | `invoke_under_slo` | ler 29 MB + pontuar abaixo do SLA (padrão 60 s) |
# MAGIC | `wrong_hash_400` | integridade: `payload_hash` divergente → 400 |
# MAGIC | `inline_rejected` | 29 MB inline são recusados (limite de 16 MB) — por isso o caminho |
# MAGIC
# MAGIC Qualquer check com FAIL faz o job falhar. O resultado volta em `notebook_output`.

# COMMAND ----------

dbutils.widgets.text("catalog", "")
dbutils.widgets.text("schema", "")
dbutils.widgets.text("payload_volume", "payloads")
dbutils.widgets.text("endpoint_name", "")
dbutils.widgets.text("secret_scope", "mlops-large-payload-modelserving")
dbutils.widgets.text("payload_sizes_mb", "0.01,15,22,29,32")
dbutils.widgets.text("inline_negative_mb", "29")
dbutils.widgets.text("slo_ms", "60000")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
endpoint_name = dbutils.widgets.get("endpoint_name")
secret_scope = dbutils.widgets.get("secret_scope")
payload_sizes_mb = [float(x) for x in dbutils.widgets.get("payload_sizes_mb").split(",") if x.strip()]
inline_negative_mb = float(dbutils.widgets.get("inline_negative_mb") or 0)
slo_ms = float(dbutils.widgets.get("slo_ms"))
volume_root = f"/Volumes/{catalog}/{schema}/{dbutils.widgets.get('payload_volume')}"

# COMMAND ----------

import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from databricks.sdk import WorkspaceClient

root = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "src" / "common" / "payloads.py").exists())
sys.path.insert(0, str(root))
from src.common.payloads import make_request_body, serialize  # noqa: E402

host = WorkspaceClient().config.host.rstrip("/")
serving_url = f"{host}/serving-endpoints/{endpoint_name}/invocations"
results: list[dict[str, Any]] = []
latency_rows: list[dict[str, Any]] = []


def record(size_mb: float | None, check: str, passed: bool, detail: str, latency_ms: float | None = None) -> None:
    results.append({"size_mb": size_mb, "check": check, "passed": "PASS" if passed else "FAIL",
                    "detail": detail[:1500], "latency_ms": None if latency_ms is None else round(latency_ms, 1)})


def oauth_token() -> str:
    resp = requests.post(
        f"{host}/oidc/v1/token",
        auth=(dbutils.secrets.get(secret_scope, "client-id"), dbutils.secrets.get(secret_scope, "client-secret")),
        data={"grant_type": "client_credentials", "scope": "all-apis"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


headers = {"Authorization": f"Bearer {oauth_token()}", "Content-Type": "application/json"}


def write_request(body: bytes) -> tuple[str, str, float]:
    """Grava request.json no mesmo layout que a Lambda de entrada usa no bucket (jobs montam /Volumes)."""
    request_id = str(uuid.uuid4())
    path = Path(f"{volume_root}/dt={datetime.now(timezone.utc):%Y-%m-%d}/{request_id}/request.json")
    t0 = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return request_id, str(path), (time.time() - t0) * 1000.0


def invoke(row: dict[str, Any], timeout: float = 300) -> tuple[int, Any, float, int]:
    data = json.dumps({"dataframe_records": [row]}).encode("utf-8")
    t0 = time.time()
    resp = requests.post(serving_url, headers=headers, data=data, timeout=(30, timeout))
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text[:500]
    return resp.status_code, body, (time.time() - t0) * 1000.0, len(data)


def prediction_of(body: Any) -> Any:
    preds = body.get("predictions") if isinstance(body, dict) else None
    return preds[0].get("prediction") if isinstance(preds, list) and preds else None


# COMMAND ----------

# Aquecimento: com scale-to-zero, o endpoint pode levar minutos para subir.
rid, uri, _ = write_request(make_request_body(0.01, seed=1))
t0 = time.time()
status, body = None, None
while time.time() - t0 < 900:
    try:
        status, body, _, _ = invoke({"file_path": uri, "request_id": rid}, timeout=120)
    except requests.RequestException as exc:
        status, body = None, str(exc)
    if status == 200:
        break
    time.sleep(15)
record(None, "warmup", status == 200, f"HTTP {status} após {time.time() - t0:.0f} s", (time.time() - t0) * 1000.0)

# COMMAND ----------

for size_mb in payload_sizes_mb:
    file_bytes = make_request_body(size_mb, seed=int(size_mb * 100) + 1)
    rid, uri, write_ms = write_request(file_bytes)
    status, resp, invoke_ms, sent = invoke({"file_path": uri, "request_id": rid})
    record(size_mb, "body_lt_1mib", sent < 1024 * 1024, f"corpo = {sent} bytes para um arquivo de {len(file_bytes)} bytes")
    record(size_mb, "http_200_prediction", status == 200 and prediction_of(resp) is not None, f"HTTP {status} {str(resp)[:300]}", invoke_ms)
    record(size_mb, "invoke_under_slo", invoke_ms < slo_ms, f"{invoke_ms:.0f} ms (SLO {slo_ms:.0f} ms)", invoke_ms)
    latency_rows.append({"size_mb": size_mb, "request_body_bytes": sent, "volume_write_ms": round(write_ms),
                         "serving_read_and_score_ms": round(invoke_ms)})

# COMMAND ----------

# Hash divergente → 400 (contrato legado: variáveis no corpo, arquivo só com o JSON do birô).
_, bureau_uri, _ = write_request(serialize({"items": [{"amount": 1.0, "category": "A"}]}))
row = {**{f"var_{i:02d}": 0.1 for i in range(1, 16)}, "payload_uri": bureau_uri, "payload_hash": "sha256:" + "0" * 64}
status, resp, ms, _ = invoke(row)
record(None, "wrong_hash_400", status == 400, f"HTTP {status} {str(resp)[:300]}", ms)


def inline_row(size_mb: float) -> dict[str, Any]:
    envelope = json.loads(make_request_body(size_mb, seed=7))
    return {**{k: v for k, v in envelope.items() if k != "payload"}, "payload": json.dumps(envelope["payload"])}


if inline_negative_mb > 0:
    # Controle: o mesmo contrato inline, pequeno, tem de funcionar (auth, rede e schema ok).
    control_status, _, _, _ = invoke(inline_row(1), timeout=120)
    # Acima de 16 MiB o gateway responde 400 ("Request size cannot exceed 16777216 bytes") ou, de dentro
    # da Databricks, fecha a conexão antes do fim do upload (sem status legível). As duas são recusa.
    outcomes, status, ms = [], None, None
    for _ in range(3):
        try:
            status, resp, ms, sent = invoke(inline_row(inline_negative_mb), timeout=180)
            outcomes.append(f"HTTP {status} ({sent} bytes) {str(resp)[:200]}")
            break
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as exc:
            outcomes.append(f"conexão fechada durante o upload ({type(exc).__name__})")
    rejected = status is None or 400 <= status < 500
    record(inline_negative_mb, "inline_rejected", control_status == 200 and rejected,
           f"controle 1 MB inline → HTTP {control_status}; {inline_negative_mb} MB inline → {outcomes}", ms)

# COMMAND ----------

latency = pd.DataFrame(latency_rows)
summary = pd.DataFrame(results)
display(latency)
display(summary)
failed = summary[summary["passed"] == "FAIL"]
if len(failed):
    raise RuntimeError("Validação falhou: " + "; ".join(f"{r.size_mb}/{r.check}: {r.detail[:300]}" for r in failed.itertuples()))
dbutils.notebook.exit(json.dumps({"latency": latency_rows, "checks": summary.to_dict("records")}))
