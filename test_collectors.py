"""Tests for the collectors and the report. Run with: python3 -m pytest"""

import json
import tempfile
import unittest
from pathlib import Path

import carbon_common as cc
import carbon_copilot
import carbon_gha
import carbon_report
import carbon_slurm
import carbon_vscode
import token_carbon as tc

JOB_REPORT = """elapsed=602s end=Sat Oct  3 17:49:24 AEST 2026

+------------------ Job Report: 17938486 (COMPLETED) ------------------+
| Memory (RAM)  [####                ] 20.2% (1.6 GB peak / 8 GB)      |
| CPU           [######              ] 33.0% average                   |
| GPU           [####                ] 24.4% average                   |
| Time          [------>             ] 34.1% (0-00:10:14 / 0-00:30:00) |
|                                                                      |
| Lustre Filesystem:                                                   |
|   /fred   733.4 MB      1.2 MB         8.8 K                         |
+----------------------------------------------------------------------+
"""

NO_DATA_REPORT = """+------------------ Job Report: 18077994 (COMPLETED) ------------------+
| Memory (RAM)  No data available                                      |
| CPU           [##                  ] 14.2% average                   |
| Time          [>                   ]  0.5% (0-00:00:37 / 0-02:00:00) |
+----------------------------------------------------------------------+
"""

SACCT = """17938486|17938486|dpg_prof|00:10:14|billing=4,cpu=4,gres/gpu=1,gres/tmp=262144000,mem=8G,node=1|14:34.703|COMPLETED|2026-10-03T17:39:10|2026-10-03T17:49:24
17938486.batch|17938486.batch|batch|00:10:14|cpu=4,gres/gpu=1,mem=8G,node=1|00:06.958|COMPLETED|2026-10-03T17:39:10|2026-10-03T17:49:24
17852790_0|17852793|pionier-gp|00:06:33|billing=4,cpu=4,mem=32G,node=1|00:00:00|COMPLETED|2026-10-01T21:42:07|2026-10-01T21:48:40
"""


def ops(*lines):
    return "".join(json.dumps(x) + "\n" for x in lines)


class VSCodeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.storage = self.root / "User"

    def tearDown(self):
        self.tmp.cleanup()

    def workspace(self, name, meta, sessions):
        ws = self.storage / "workspaceStorage" / name
        (ws / "chatSessions").mkdir(parents=True)
        (ws / "workspace.json").write_text(json.dumps(meta))
        for sid, text in sessions.items():
            (ws / "chatSessions" / f"{sid}.jsonl").write_text(text)

    def session(self):
        # A request whose counts are set, then reset as the response streams;
        # a second request removed by a truncation (an undone turn) and a third.
        return ops(
            {"kind": 0, "v": {"sessionId": "s1", "requests": []}},
            {"kind": 2, "k": ["requests"], "v": [
                {"requestId": "rA", "timestamp": 1789804064051, "modelId": "copilot/auto"}]},
            {"kind": 1, "k": ["requests", 0, "completionTokens"], "v": 100},
            {"kind": 1, "k": ["requests", 0, "promptTokens"], "v": 1000},
            {"kind": 1, "k": ["requests", 0, "result"], "v": {"metadata": {
                "resolvedModel": "gpt-5.6-terra", "promptTokens": 1000, "outputTokens": 90,
                "toolCallRounds": [{}, {}, {}]}}},
            {"kind": 1, "k": ["requests", 0, "completionTokens"], "v": 250},
            {"kind": 1, "k": ["requests", 0, "copilotCredits"], "v": 1.5},
            {"kind": 2, "k": ["requests"], "v": [
                {"requestId": "rB", "timestamp": 1789804100000, "modelId": "copilot/auto",
                 "result": {"details": "Claude Sonnet 5 • 1.0 credits"}}]},
            {"kind": 1, "k": ["requests", 1, "promptTokens"], "v": 2000},
            {"kind": 1, "k": ["requests", 1, "completionTokens"], "v": 50},
            {"kind": 2, "k": ["requests"], "i": 1},
            {"kind": 2, "k": ["requests"], "v": [
                {"requestId": "rC", "timestamp": 1789804200000, "modelId": "copilot/auto"}]},
            {"kind": 2, "k": ["requests", 1, "response"], "v": [{"value": "x"}]},
        )

    def test_replay_counts_each_request_once(self):
        wfile = self.root / "Workspaces" / "1" / "workspace.json"
        wfile.parent.mkdir(parents=True)
        wfile.write_text(json.dumps({"folders": [{"path": "/x/other"}, {"path": "/x/demo"}]}))
        self.workspace("h1", {"folder": "file:///x/demo"}, {"s1": self.session()})
        self.workspace("h2", {"workspace": wfile.as_uri()}, {"s2": ops(
            {"kind": 0, "v": {"requests": [{"requestId": "rD", "timestamp": 1789804300000,
                                            "promptTokens": 10, "completionTokens": 1}]}})})
        self.workspace("h3", {"folder": "file:///x/unrelated"}, {"s3": ops(
            {"kind": 0, "v": {"requests": [{"requestId": "rE", "timestamp": 1789804300000,
                                            "copilotCredits": 4.5}]}})})
        out = carbon_vscode.collect(["/x/demo"], self.storage)
        recs = out["vscode_copilot"]
        self.assertEqual(set(recs), {"rA", "rB", "rC", "rD"})
        a = recs["rA"]
        self.assertEqual((a["prompt"], a["output"], a["rounds"]), (1000, 250, 3))
        self.assertEqual(a["model"], "gpt-5.6-terra")
        self.assertEqual(a["day"], "2026-09-19")
        self.assertEqual(recs["rB"]["model"], "claude-sonnet-5")
        self.assertEqual(recs["rB"]["prompt"], 2000)
        self.assertFalse(recs["rC"]["has_tokens"])
        months = out["vscode_months"]
        self.assertEqual(months["2026-09|h3"]["credits"], 4.5)
        self.assertFalse(months["2026-09|h3"]["repo"])
        self.assertEqual(months["2026-09|h1"]["requests"], 3)

    def test_model_classes(self):
        self.assertEqual(carbon_vscode.model_class("claude-sonnet-5")[0], ("sonnet",) * 3)
        self.assertEqual(carbon_vscode.model_class("gpt-5.6-terra")[0][1], "opus")
        self.assertEqual(carbon_vscode.model_class("gpt-5.6-luna")[0][1], "sonnet")
        self.assertEqual(carbon_vscode.model_class("gpt-5.5-2026-04-23")[0][1], "opus")
        self.assertEqual(carbon_vscode.model_class("something-new")[0][1], "opus")

    def test_request_energy_by_hand(self):
        recs = {"rA": {"model": "gpt-5.6-terra", "prompt": 1000, "output": 250, "rounds": 3,
                       "has_tokens": True, "ts": 0, "day": "2026-09-19"}}
        item = carbon_vscode.cost(recs)[0]
        # Opus class: 1000 x 238 + 250 x 5050 Wh per million = 1.5005 server Wh.
        self.assertAlmostEqual(item["kwh"][1], 1.5005 * 1.14 / 1000)
        self.assertAlmostEqual(item["kg"][1], 1.5005 * tc.CO2_G_PER_WH / 1000)
        # High: prompt x (3 + 1) / 2, still Opus.
        self.assertAlmostEqual(item["kwh"][2], (2000 * 238 + 250 * 5050) / 1e6 * 1.14 / 1000)
        # Low: Sonnet class.
        self.assertAlmostEqual(item["kwh"][0], (1000 * 119 + 250 * 2525) / 1e6 * 1.14 / 1000)

    def test_cached_fraction_and_imputation(self):
        recs = {f"r{i}": {"model": "claude-opus-5", "prompt": 1000 * (i + 1), "output": 10,
                          "rounds": 1, "has_tokens": True, "ts": i, "day": "2026-09-19"}
                for i in range(3)}
        recs["rX"] = {"model": "claude-opus-5", "prompt": 0, "output": 0, "rounds": 1,
                      "has_tokens": False, "ts": 9, "day": "2026-09-19"}
        params = dict(carbon_vscode.PARAMS, cached_fraction=0.5)
        items = {i["id"]: i for i in carbon_vscode.cost(recs, params=params)}
        x = items["rX"]
        self.assertEqual(x["tokens"]["prompt"], 2000)  # median of all, too few peers
        self.assertEqual(x["kwh"][0], 0.0)
        wh = (1000 * 238 + 1000 * 238 * 0.08 + 10 * 5050) / 1e6
        self.assertAlmostEqual(items["r1"]["kwh"][1], wh * 1.14 / 1000)


class SlurmTest(unittest.TestCase):
    def test_parse_job_report(self):
        (r,) = carbon_slurm.parse_job_reports(JOB_REPORT)
        self.assertEqual(r["jobid"], "17938486")
        self.assertEqual(r["state"], "COMPLETED")
        self.assertEqual((r["cpu_pct"], r["gpu_pct"]), (33.0, 24.4))
        self.assertEqual((r["mem_peak_gb"], r["mem_alloc_gb"]), (1.6, 8.0))
        self.assertEqual((r["report_elapsed_s"], r["timelimit_s"]), (614.0, 1800.0))

    def test_parse_job_report_without_data(self):
        (r,) = carbon_slurm.parse_job_reports(NO_DATA_REPORT)
        self.assertNotIn("mem_alloc_gb", r)
        self.assertNotIn("gpu_pct", r)
        self.assertFalse(r["has_gpu_line"])
        self.assertEqual(r["cpu_pct"], 14.2)
        self.assertEqual(r["report_elapsed_s"], 37.0)

    def test_report_records_from_array_log_name(self):
        recs = carbon_slurm.report_records(NO_DATA_REPORT, "/x/logs/virgil-nb-18077914_28.out")
        self.assertEqual(recs["18077994"]["name"], "virgil-nb")
        self.assertEqual(recs["18077994"]["master"], "18077914")

    def test_parse_sacct(self):
        jobs = carbon_slurm.parse_sacct(SACCT)
        self.assertEqual(set(jobs), {"17938486", "17852793"})
        j = jobs["17938486"]
        self.assertEqual((j["ncpu"], j["ngpu"], j["mem_alloc_gb"]), (4, 1, 8.0))
        self.assertAlmostEqual(j["totalcpu_s"], 874.703)
        self.assertEqual(j["elapsed_s"], 614.0)
        self.assertEqual(jobs["17852793"]["master"], "17852790")
        self.assertEqual(jobs["17852793"]["ngpu"], 0)

    def test_durations(self):
        self.assertEqual(carbon_slurm.duration_s("1-02:03:04"), 93784)
        self.assertEqual(carbon_slurm.duration_s("10:14"), 614)
        self.assertAlmostEqual(carbon_slurm.duration_s("14:34.703"), 874.703)

    def test_submissions_ledger(self):
        subs = carbon_slurm.parse_submissions(
            "18061096\tjob.sbatch\t0-3\tvirgil@5c9da1b\t-\t2026-10-05_17:01:00\n")
        self.assertEqual(subs["18061096"]["sha"], "5c9da1b")
        self.assertEqual(subs["18061096"]["lib"], "virgil")

    def test_gpu_job_energy_by_hand(self):
        # dpg_prof 17938486: 10 min 14 s on 4 EPYC 7543 cores at 33% and one
        # A100 at 24.4%, 8 GB: 614/3600 h x (4 x 7.03125 x 0.33 + 400 x 0.244
        # + 8 x 0.3725) W = 18.737 Wh, x PUE 1.67 = 31.29 Wh.
        job = {}
        cc.merge_records(job, carbon_slurm.report_records(JOB_REPORT, "/x/dpg_prof-17938486.out"))
        cc.merge_records(job, carbon_slurm.parse_sacct(SACCT))
        kwh, gpu = carbon_slurm.job_kwh(job["17938486"])
        self.assertTrue(gpu)
        expected = 614 / 3600 * (4 * 225 / 32 * 0.33 + 400 * 0.244 + 8 * 0.3725) * 1.67 / 1000
        self.assertAlmostEqual(expected, 0.03129, places=5)
        self.assertEqual(kwh, [expected] * 3)
        item = next(i for i in carbon_slurm.cost(job, {}) if i["id"] == "17938486")
        self.assertAlmostEqual(item["kg"][1], expected * 0.74)

    def test_cpu_usage_falls_back_to_totalcpu(self):
        job = carbon_slurm.parse_sacct(SACCT)["17938486"]
        kwh, _ = carbon_slurm.job_kwh(job)
        u_cpu = 874.703 / (614 * 4)
        lo = 614 / 3600 * (4 * 225 / 32 * u_cpu + 400 * 0.25 + 8 * 0.3725) * 1.67 / 1000
        self.assertAlmostEqual(kwh[0], lo)

    def test_classification(self):
        classes = {"science": {"jobs": ["apep*"], "dirs": ["*nuHor*"]},
                   "validation": {"jobs": ["sbc*"]}}
        self.assertEqual(cc.classify(classes, job="apep_fit"), "science")
        self.assertEqual(cc.classify(classes, job="x", dir="/a/nuHor/logs"), "science")
        self.assertEqual(cc.classify(classes, job="sbc_numpyro"), "validation")
        self.assertEqual(cc.classify(classes, job="dpg_prof"), "dev")


class GHATest(unittest.TestCase):
    def test_runner_energy_by_hand(self):
        # One hour of a 4-vCPU, 16 GB Linux runner at full use:
        # (4 x 4.375 + 16 x 0.3725) W x 1.18 = 27.6828 Wh.
        item = carbon_gha.cost({"r#1": {"workflow": "tests", "day": "2026-10-01",
                                        "head_branch": "x", "runner_s": {"ubuntu": 3600}}})[0]
        self.assertAlmostEqual(item["kwh"][1], 0.0276828)
        self.assertAlmostEqual(item["kwh"][0], (2 * 4.375 + 16 * 0.3725) * 1.18 / 1000)

    def test_pr_number(self):
        self.assertEqual(carbon_gha.pr_number({"prs": [7]}), 7)
        self.assertEqual(carbon_gha.pr_number({"head_branch": "refs/pull/34/head"}), 34)
        self.assertEqual(carbon_gha.pr_number({"title": "Addressing comment on PR #201"}), 201)
        self.assertIsNone(carbon_gha.pr_number({"head_branch": "main"}))


class CopilotTest(unittest.TestCase):
    def sources(self):
        return {
            "copilot_billing": {
                "a": {"day": "2026-09-01", "month": "2026-09", "sku": "Copilot AI Credits",
                      "unit": "AICredits", "quantity": 1000.0, "price": 0.01, "repo": ""},
                "b": {"day": "2026-09-01", "month": "2026-09", "sku": "Copilot Cloud Agent",
                      "unit": "AICredits", "quantity": 200.0, "price": 0.01, "repo": ""},
                "c": {"day": "2026-10-01", "month": "2026-10", "sku": "Copilot AI Credits",
                      "unit": "AICredits", "quantity": 500.0, "price": 0.01, "repo": "demo"},
            },
            "copilot_agent_prs": {
                "me/demo#1": {"repo": "me/demo", "month": "2026-09"},
                "me/other#2": {"repo": "me/other", "month": "2026-09"},
            },
            "vscode_months": {
                "2026-09|h1": {"month": "2026-09", "repo": True, "credits": 300.0, "requests": 3,
                               "prompt": 300_000, "output": 10_000},
                "2026-09|h2": {"month": "2026-09", "repo": False, "credits": 100.0, "requests": 1,
                               "prompt": 100_000, "output": 0},
            },
            "copilot_runs": {
                "me/demo#5": {"repo": "me/demo", "kind": "cloud agent", "workflow": "Copilot cloud agent",
                              "day": "2026-09-10", "head_branch": "copilot/fix", "pr": None,
                              "duration_ms": 600_000},
                "me/demo#6": {"repo": "me/demo", "kind": "code review", "workflow": "Copilot",
                              "day": "2026-08-10", "head_branch": "feat", "pr": 3,
                              "duration_ms": 60_000},
            },
        }

    def test_billing_attribution(self):
        src = self.sources()
        cal = carbon_copilot.calibration(src["vscode_months"], {})
        # 400 credits for 410,000 tokens.
        self.assertAlmostEqual(cal["credits_per_mtok"], 400 / 0.41)
        sep = carbon_copilot.attribute_month(
            "2026-09", "me/demo", src["copilot_billing"], {}, src["copilot_agent_prs"],
            src["copilot_runs"], src["vscode_months"], cal, carbon_copilot.PARAMS)
        # Review: (1000 - 400 local credits) x 300/400 local share = 450 credits.
        self.assertIn("450 AI Credits", sep["code review"][2])
        # Agent: 200 x 1/2 agent PRs = 100 credits.
        self.assertIn("100 AI Credits", sep["cloud agent"][2])
        tokens = 450 / cal["credits_per_mtok"] * 1e6
        f = cal["output_fraction"]
        expect = cc.llm_cost("sonnet", {"input": tokens * (1 - f), "output": tokens * f})
        self.assertAlmostEqual(sep["code review"][0][1], expect[0])
        octo = carbon_copilot.attribute_month(
            "2026-10", "me/demo", src["copilot_billing"], {}, src["copilot_agent_prs"],
            src["copilot_runs"], src["vscode_months"], cal, carbon_copilot.PARAMS)
        self.assertIn("500 AI Credits", octo["code review"][2])

    def test_headline_uses_billing_where_available(self):
        items, check, _ = carbon_copilot.cost(self.sources(), "me/demo")
        by_id = {i["id"]: i for i in items}
        self.assertEqual(by_id["me/demo#5"]["basis"], "billing")
        self.assertEqual(by_id["me/demo#6"]["basis"], "run time")
        # September review credits have no run to sit on, so they stand alone.
        self.assertIn("me/demo|2026-09|code review", by_id)
        # A one-minute review on run time: 21,000 prompt and 640 output tokens on Sonnet,
        # plus one minute of runner.
        llm = cc.llm_cost("sonnet", {"input": 21_000, "output": 640})[0]
        runner = carbon_gha.runner_kwh({"ubuntu": 60}, 1.0, 1.18)
        self.assertAlmostEqual(by_id["me/demo#6"]["kwh"][1], llm + runner)
        self.assertEqual({c["headline"] for c in check if c["month"] == "2026-09"}, {"billing"})


class LedgerAndReportTest(unittest.TestCase):
    def test_merge_keeps_maxima_and_old_records(self):
        old = {"a": {"n": 5, "state": "RUNNING"}, "b": {"n": 1}}
        cc.merge_records(old, {"a": {"n": 3, "state": "COMPLETED"}, "c": {"n": 2}})
        self.assertEqual(old, {"a": {"n": 5, "state": "COMPLETED"}, "b": {"n": 1}, "c": {"n": 2}})

    def test_report_from_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "ledger.json"
            cc.update_ledger(ledger, {
                "claude": {"s|2026-09-30|claude-opus-5-5|feat": {
                    "session": "s", "day": "2026-09-30", "model": "claude-opus-5-5",
                    "branch": "feat", "cwd": "/x/demo", "input": 0, "cache_write": 0,
                    "cache_read": 0, "output": 1_000_000, "calls": 1}},
                "slurm": {"1": {"name": "apep_fit", "elapsed_s": 3600, "ncpu": 1, "ngpu": 0,
                                "mem_alloc_gb": 0, "cpu_pct": 100.0, "day": "2026-09-30"},
                          "2": {"name": "dpg_prof", "elapsed_s": 3600, "ncpu": 1, "ngpu": 0,
                                "mem_alloc_gb": 0, "cpu_pct": 100.0, "day": "2026-09-30",
                                "sha": "abc"}},
                "prs": {"9": {"number": 9, "title": "Add a feature", "branch": "feat",
                              "created": "2026-09-29"}},
                "sha_prs": {"abc": {"prs": [9]}},
            })
            cfg_path = Path(d) / "c.json"
            cfg_path.write_text(json.dumps({
                "repo": "me/demo", "classes": {"science": {"jobs": ["apep*"]}}}))
            md, summary = Path(d) / "page.md", Path(d) / "s.json"
            carbon_report.main(["--config", str(cfg_path), "--ledger", str(ledger), "--offline",
                                "--markdown", str(md), "--summary", str(summary)])
            s = json.loads(summary.read_text())
            claude_kg = 5050 * tc.CO2_G_PER_WH / 1000
            job_kg = 225 / 32 * 1.67 / 1000 * 0.74
            self.assertAlmostEqual(s["total"]["kg"][1], claude_kg + job_kg)
            self.assertEqual(list(s["by_feature"]), ["#9 Add a feature"])
            self.assertEqual(s["by_feature"]["#9 Add a feature"]["n"], 2)
            self.assertIn("science", s["by_class"])
            page = md.read_text()
            self.assertIn("[#9](https://github.com/me/demo/pull/9) Add a feature", page)
            self.assertIn("Data analysis runs (not in the total)", page)
            self.assertNotIn("!!!", page)
            self.assertTrue(s["badge"]["text"].startswith("dev carbon | 1.9"))


if __name__ == "__main__":
    unittest.main()
