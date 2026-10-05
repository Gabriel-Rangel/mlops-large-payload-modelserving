"""Lambdas do exemplo AWS com boto3 e HTTP simulados."""

from __future__ import annotations

import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "aws"), str(ROOT / "src" / "common")]
os.environ.update(
    {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
        "BUCKET": "bucket",
        "QUEUE_URL": "https://sqs/requests",
        "INBOX_URL": "https://sqs/inbox",
        "S3_PREFIX": "payloads/",
        "VOLUME_ROOT": "/Volumes/cat/sch/payloads",
        "ENDPOINT_NAME": "ep",
        "DATABRICKS_SECRET_ID": "secret",
    }
)


def _import(name: str):
    with mock.patch("boto3.client"):
        return importlib.reload(importlib.import_module(name))


class IngressTests(unittest.TestCase):
    def setUp(self):
        self.mod = _import("ingress_lambda")

    def test_uploads_devolve_url_e_caminho_no_volume(self):
        self.mod.s3.generate_presigned_url.return_value = "https://signed"
        out = json.loads(self.mod.handler({"routeKey": "POST /uploads"}, None)["body"])
        self.assertEqual(out["upload_url"], "https://signed")
        self.assertTrue(out["s3_key"].startswith("payloads/dt="))
        self.assertEqual(out["file_path"], "/Volumes/cat/sch/payloads/" + out["s3_key"][len("payloads/"):])

    def test_decisao_exige_upload_antes(self):
        from botocore.exceptions import ClientError

        self.mod.s3.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")
        event = {"routeKey": "POST /credit-decision", "body": json.dumps({"request_id": "r1"})}
        self.assertEqual(self.mod.handler(event, None)["statusCode"], 409)

    def test_decisao_poe_so_o_ponteiro_na_fila(self):
        key = self.mod.request_key("r1")
        event = {"routeKey": "POST /credit-decision", "body": json.dumps({"request_id": "r1", "s3_key": key})}
        self.assertEqual(self.mod.handler(event, None)["statusCode"], 202)
        self.assertEqual(json.loads(self.mod.sqs.send_message.call_args.kwargs["MessageBody"]), {"request_id": "r1", "s3_key": key})

    def test_request_pequeno_inline_e_gravado(self):
        body = json.dumps({"var_01": 1, "payload": {"items": []}, "request_id": "r2"})
        self.assertEqual(self.mod.handler({"routeKey": "POST /credit-decision", "body": body}, None)["statusCode"], 202)
        self.assertEqual(self.mod.s3.put_object.call_args.kwargs["Body"], body.encode())


class InferenceTests(unittest.TestCase):
    def setUp(self):
        self.mod = _import("inference_lambda")
        self.http = mock.patch.object(self.mod.dbx_http, "http").start()
        mock.patch.object(self.mod.dbx_http, "databricks_host", return_value="https://ws").start()
        mock.patch.object(self.mod.dbx_http, "databricks_token", return_value="tok").start()
        self.addCleanup(mock.patch.stopall)

    def _event(self):
        msg = {"request_id": "r1", "s3_key": "payloads/dt=2026-10-05/r1/request.json"}
        return {"Records": [{"messageId": "m1", "body": json.dumps(msg)}]}

    def test_envia_file_path_e_grava_resposta(self):
        self.http.return_value = (200, {}, json.dumps({"predictions": [{"prediction": 1, "probability": 0.8}]}).encode())
        self.assertEqual(self.mod.handler(self._event(), None), {"batchItemFailures": []})
        sent = json.loads(self.http.call_args.args[2])
        self.assertEqual(sent["dataframe_records"][0]["file_path"], "/Volumes/cat/sch/payloads/dt=2026-10-05/r1/request.json")
        put = self.mod.s3.put_object.call_args.kwargs
        self.assertEqual(put["Key"], "payloads/dt=2026-10-05/r1/response_dbx.json")
        self.assertEqual(json.loads(put["Body"])["status"], "done")

    def test_5xx_volta_para_a_fila(self):
        self.http.return_value = (503, {}, b"scaling from zero")
        self.assertEqual(self.mod.handler(self._event(), None), {"batchItemFailures": [{"itemIdentifier": "m1"}]})
        self.mod.s3.put_object.assert_not_called()

    def test_4xx_gera_decisao_failed(self):
        self.http.return_value = (400, {}, b"payload_hash divergente")
        self.assertEqual(self.mod.handler(self._event(), None), {"batchItemFailures": []})
        self.assertEqual(json.loads(self.mod.s3.put_object.call_args.kwargs["Body"])["status"], "failed")


class SimulatorTests(unittest.TestCase):
    def test_evento_s3_cru_e_envelope_sns(self):
        sim = _import("credit_engine_sim")
        raw = json.dumps({"Records": [{"s3": {"object": {"key": "payloads/dt%3D2026-10-05/r1/response_dbx.json"}}}]})
        expected = ["payloads/dt=2026-10-05/r1/response_dbx.json"]
        self.assertEqual(sim.s3_keys(raw), expected)
        self.assertEqual(sim.s3_keys(json.dumps({"Message": raw})), expected)
        self.assertEqual(sim.s3_keys(json.dumps({"Event": "s3:TestEvent"})), [])


if __name__ == "__main__":
    unittest.main()
