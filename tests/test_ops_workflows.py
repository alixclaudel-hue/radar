"""Cartographie des requêtes : ingestion des transcripts et lecture côté radar_ops."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)

import workflow_ingest as wi  # noqa: E402
from radar_ops import workflows  # noqa: E402


def ep(hms):
    return datetime.fromisoformat(f"2026-09-27T{hms}+00:00").timestamp()


def iso(hms):
    return f"2026-09-27T{hms}.000Z"


USAGE = {"input_tokens": 10, "cache_read_input_tokens": 1000,
         "cache_creation_input_tokens": 100, "output_tokens": 5}


def user(hms, content, **kw):
    return dict({"type": "user", "timestamp": iso(hms), "sessionId": "sess1",
                 "cwd": "/p/radar-work", "message": {"role": "user", "content": content}}, **kw)


def assistant(hms, rid, *blocks):
    return {"type": "assistant", "timestamp": iso(hms), "sessionId": "sess1", "requestId": rid,
            "message": {"model": "claude-x", "usage": USAGE, "content": list(blocks)}}


def result(hms, tid, text, err=False):
    return user(hms, [{"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": err}])


TRANSCRIPT = [
    user("10:00:00", "Fais X"),
    assistant("10:00:01", "r1", {"type": "text", "text": "Je délègue."}),
    assistant("10:00:02", "r1", {"type": "tool_use", "id": "t1", "name": "Bash",
                                 "input": {"command": "python3 scripts/ai_broker.py --mode context -f a.py"}}),
    result("10:00:30", "t1", "résumé JSON"),
    assistant("10:00:40", "r2", {"type": "tool_use", "id": "t2", "name": "Read",
                                 "input": {"file_path": "/p/a.py"}}),
    result("10:00:41", "t2", "introuvable", err=True),
    user("10:01:00", "<task-notification><task-id>x</task-id><status>completed</status></task-notification>"),
    assistant("10:01:05", "r3", {"type": "text", "text": "Fini"}),
    user("10:02:00", "<command-name>/clear</command-name>"),
    user("10:03:00", "Autre"),
    assistant("10:03:05", "r4", {"type": "text", "text": "OK"}),
    {"type": "assistant", "isSidechain": True, "timestamp": iso("10:03:06"), "requestId": "side",
     "message": {"usage": USAGE, "content": []}},
]
RECEIPTS = [
    {"ts": ep("10:00:31"), "mode": "context", "status": "ok", "provider": "openrouter",
     "model": "m1", "prompt_tokens": 500, "output_tokens": 50, "cost_usd": 0.0},
    {"ts": ep("11:00:00"), "mode": "pr", "status": "ok", "provider": "gemini",
     "model": "m2", "prompt_tokens": 9, "output_tokens": 9},
]
ATTEMPTS = [
    {"ts": ep("10:00:10"), "mode": "context", "provider": "gemini", "model": "g1", "status": "error",
     "error_type": "invalid_json", "tokens": {"prompt_tokens": 400, "output_tokens": 0}},
    {"ts": ep("10:00:29"), "mode": "context", "provider": "openrouter", "model": "m1", "status": "ok",
     "tokens": {"prompt_tokens": 500, "output_tokens": 50}},
]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "sess1.jsonl")
        with open(self.path, "w", encoding="utf-8") as f:
            for line in TRANSCRIPT:
                f.write(json.dumps(line) + "\n")
            f.write("{ligne corrompue\n")

    def parse(self, **kw):
        return wi.parse_transcript(self.path, RECEIPTS, ATTEMPTS, **kw)


class IngestionTests(Base):
    def test_decoupage_en_requetes(self):
        reqs = self.parse()
        self.assertEqual([r["id"] for r in reqs], ["sess1:1", "sess1:2"])
        self.assertEqual(reqs[0]["project"], "/p/radar-work")
        self.assertEqual(reqs[1]["title"], "Autre")

    def test_usage_compte_une_fois_par_appel_api(self):
        c = self.parse()[0]["totals"]["claude"]
        self.assertEqual(c["calls"], 3)
        self.assertEqual(c["cache_read"], 3000)
        self.assertEqual(c["output"], 15)

    def test_ordre_chronologique_des_etapes(self):
        kinds = [s["kind"] for s in self.parse()[0]["steps"]]
        self.assertEqual(kinds, ["prompt", "claude_call", "broker", "claude_call", "tool",
                                 "notification", "claude_call", "answer"])

    def test_appel_courtier_rattache_au_recu_et_aux_tentatives(self):
        req = self.parse()[0]
        broker = next(s for s in req["steps"] if s["kind"] == "broker")
        self.assertEqual(broker["label"], "mode context")
        self.assertEqual(broker["receipt"]["model"], "m1")
        self.assertEqual(broker["tokens"], {"prompt": 500, "output": 50})
        self.assertEqual(broker["output"], "résumé JSON")
        self.assertEqual(len(broker["attempts"]), 2)
        d = req["totals"]["delegated"]
        self.assertEqual((d["calls"], d["with_receipt"], d["attempts_failed"]), (1, 1, 1))

    def test_recu_hors_fenetre_non_rattache(self):
        steps = [s for r in self.parse() for s in r["steps"]]
        self.assertFalse(any((s.get("receipt") or {}).get("mode") == "pr" for s in steps))

    def test_erreur_outil_et_reponse_finale(self):
        req = self.parse()[0]
        self.assertEqual(req["totals"]["tools"]["errors"], 1)
        self.assertEqual(req["steps"][-1]["output"], "Fini")

    def test_since_garde_les_identifiants_stables(self):
        reqs = self.parse(since_ts=ep("10:02:30"))
        self.assertEqual([r["id"] for r in reqs], ["sess1:2"])

    def test_masquage_applique(self):
        req = self.parse(redact=lambda s: s.replace("X", "*"))[0]
        self.assertEqual(req["prompt"], "Fais *")
        self.assertEqual(req["steps"][0]["input"], "Fais *")

    def test_troncature(self):
        self.assertTrue(wi.truncate("a" * 20, 10).endswith("[tronqué, 20 car. au total]"))

    def test_mode_courtier_detecte(self):
        self.assertEqual(wi.broker_mode("Bash", {"command": "python3 scripts/ai_query.py --mode=pr --stdin"}), "pr")
        self.assertIsNone(wi.broker_mode("Bash", {"command": "python3 scripts/ai_broker.py --help"}))
        self.assertIsNone(wi.broker_mode("Read", {"command": "ai_broker.py --mode code"}))

    def test_ingest_one_ecrit_le_fichier_de_session(self):
        ops = os.path.join(self.tmp.name, "ops")
        os.makedirs(os.path.join(ops, "ai"))
        with open(os.path.join(ops, "gemini-receipts.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(r) for r in RECEIPTS))
        self.assertEqual(wi.ingest_one(self.path, ops), 2)
        with open(os.path.join(ops, "workflows", "sess1.json")) as f:
            self.assertEqual(len(json.load(f)["requests"]), 2)

    def test_ingest_one_ne_leve_jamais(self):
        self.assertEqual(wi.ingest_one(os.path.join(self.tmp.name, "absent.jsonl"), self.tmp.name), 0)


class LectureOpsTests(Base):
    def setUp(self):
        super().setUp()
        self.ops = os.path.join(self.tmp.name, "ops")
        os.makedirs(os.path.join(self.ops, "workflows"))
        wi._write(self.ops, self.parse())
        with open(os.path.join(self.ops, "workflows", "casse.json"), "w") as f:
            f.write("{pas du json")
        patcher = mock.patch.dict(os.environ, {"RADAR_OPS_TELEMETRY_DIR": self.ops})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_overview_liste_et_part_deleguee(self):
        ov = workflows.overview("/inutilise")
        self.assertEqual(len(ov["requests"]), 2)
        first = next(r for r in ov["requests"] if r["id"] == "sess1:1")
        fresh = 3 * (10 + 100 + 5)
        self.assertEqual(first["claude_fresh"], fresh)
        self.assertEqual(first["delegated_tokens"], 550)
        self.assertEqual(first["share_delegated"], round(550 / (550 + fresh), 3))
        self.assertEqual(ov["projects"], ["/p/radar-work"])

    def test_sankey(self):
        s = workflows.overview("/inutilise")["sankey"]
        links = {(l["source"], l["target"]): l["value"] for l in s["links"]}
        self.assertEqual(links[("broker", "m:openrouter/m1")], 550)
        self.assertEqual(links[("broker", "failed")], 400)
        self.assertEqual(links[("req", "broker")], 950)
        self.assertTrue(all(v > 0 for v in links.values()))
        ids = {n["id"] for n in s["nodes"]}
        self.assertTrue({l["source"] for l in s["links"]} | {l["target"] for l in s["links"]} <= ids)

    def test_filtres(self):
        self.assertEqual(len(workflows.overview("/x", since_ts=ep("10:02:30"))["requests"]), 1)
        self.assertEqual(len(workflows.overview("/x", project="/autre")["requests"]), 0)

    def test_get_request(self):
        self.assertEqual(workflows.get_request("/x", "sess1:1")["title"], "Fais X")
        self.assertIsNone(workflows.get_request("/x", "sess1:99"))
        self.assertIsNone(workflows.get_request("/x", "../x:1"))
        self.assertIsNone(workflows.get_request("/x", "sansdeuxpoints"))


if __name__ == "__main__":
    unittest.main()
