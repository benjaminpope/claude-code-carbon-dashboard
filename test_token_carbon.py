"""Tests for token_carbon.py. Run with: python3 -m pytest (or python3 -m unittest -v)"""

import json
import tempfile
import unittest
from pathlib import Path

import token_carbon as tc


def call(mid, session, cwd, ts="2026-09-30T10:00:00Z", model="claude-opus-5-5",
         inp=1, cw=10, cr=100, out=5, branch="main"):
    return {
        "type": "assistant", "sessionId": session, "cwd": cwd, "timestamp": ts,
        "gitBranch": branch,
        "message": {"id": mid, "model": model, "usage": {
            "input_tokens": inp, "cache_creation_input_tokens": cw,
            "cache_read_input_tokens": cr, "output_tokens": out}},
    }


class TokenCarbonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.projects, self.data = root / "projects", root / "data"
        self.proj = self.projects / "-Users-me-code-demo"
        self.proj.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, records):
        path = self.proj / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        return path

    def run_update(self):
        return tc.tally(tc.update_history(self.data, self.projects))["total"]

    def test_streaming_duplicates_count_once(self):
        # One API call streamed over three lines; the last has the final output.
        self.write("s1.jsonl", [call("m1", "s1", "/Users/me/code/demo", out=1),
                                call("m1", "s1", "/Users/me/code/demo", out=3),
                                call("m1", "s1", "/Users/me/code/demo", out=7)])
        total = self.run_update()
        self.assertEqual(total["calls"], 1)
        self.assertEqual(total["tokens"]["output"], 7)
        self.assertEqual(total["tokens"]["cache_read"], 100)

    def test_subagent_transcripts_are_included(self):
        self.write("s1.jsonl", [call("m1", "s1", "/Users/me/code/demo")])
        self.write("s1/subagents/agent-a.jsonl", [call("m2", "s1", "/Users/me/code/demo")])
        self.assertEqual(self.run_update()["calls"], 2)

    def test_project_named_from_launch_directory(self):
        # A call made from a subfolder still belongs to the launch project.
        self.write("s1.jsonl", [call("m1", "s1", "/Users/me/code/demo/notebooks"),
                                call("m2", "s1", "/Users/me/code/demo")])
        records = tc.update_history(self.data, self.projects)
        self.assertEqual({r["project"] for r in records}, {"demo"})

    def test_history_survives_transcript_deletion(self):
        path = self.write("s1.jsonl", [call("m1", "s1", "/Users/me/code/demo")])
        self.run_update()
        path.unlink()
        self.write("s2.jsonl", [call("m2", "s2", "/Users/me/code/demo")])
        total = self.run_update()
        self.assertEqual(total["calls"], 2)
        self.assertEqual(total["tokens"]["cache_read"], 200)

    def test_growing_session_updates_without_double_counting(self):
        self.write("s1.jsonl", [call("m1", "s1", "/Users/me/code/demo")])
        self.run_update()
        self.write("s1.jsonl", [call("m1", "s1", "/Users/me/code/demo"),
                                call("m2", "s1", "/Users/me/code/demo")])
        self.assertEqual(self.run_update()["calls"], 2)
        self.assertEqual(self.run_update()["calls"], 2)

    def test_days_and_models_are_separate_records(self):
        self.write("s1.jsonl", [
            call("m1", "s1", "/Users/me/code/demo", ts="2026-09-29T23:00:00Z"),
            call("m2", "s1", "/Users/me/code/demo", ts="2026-09-30T01:00:00Z"),
            call("m3", "s1", "/Users/me/code/demo", model="claude-haiku-4-5")])
        records = tc.update_history(self.data, self.projects)
        self.assertEqual(len(records), 3)
        self.assertEqual(len(tc.select(records, since="2026-09-30")), 2)

    def test_emissions_match_tokenclimate_sheets(self):
        # Opus sheet: 90 g per million input tokens, 1.9 kg per million output.
        self.write("s1.jsonl", [call("m1", "s1", "/Users/me/code/demo",
                                     inp=1_000_000, cw=0, cr=0, out=1_000_000)])
        g = self.run_update()["g_co2e"]
        self.assertAlmostEqual(g["input"], 89.5, places=1)
        self.assertAlmostEqual(g["output"], 1899.7, places=1)

    def test_tokenclimate_worked_example(self):
        # The Sonnet session in TokenClimate's methodology: 134.06 Wh, 50.43 g.
        self.write("s1.jsonl", [call("m1", "s1", "/Users/me/code/demo", model="claude-sonnet-5-5",
                                     inp=50_000, cw=200_000, cr=3_000_000, out=30_000)])
        total = self.run_update()
        self.assertAlmostEqual(total["wh"]["total"], 134.06, places=2)
        self.assertAlmostEqual(total["g_co2e"]["total"], 50.43, places=2)

    def test_model_families(self):
        self.assertEqual(tc.model_family("claude-opus-5-5"), "opus")
        self.assertEqual(tc.model_family("claude-sonnet-5-5"), "sonnet")
        self.assertEqual(tc.model_family("claude-haiku-4-5-20251001"), "haiku")
        self.assertEqual(tc.model_family("claude-fable-5-1"), "fable")
        self.assertEqual(tc.model_family("some-gateway-model"), "opus")
        self.assertFalse(tc.is_known_family("some-gateway-model"))

    def test_each_call_uses_its_own_model_factors(self):
        self.write("s1.jsonl", [
            call("m1", "s1", "/Users/me/code/demo", model="claude-opus-5-5"),
            call("m2", "s1", "/Users/me/code/demo", model="claude-haiku-4-5")])
        by = tc.tally(tc.update_history(self.data, self.projects), "family")
        self.assertEqual(set(by), {"Claude Opus", "Claude Haiku"})
        ratio = by["Claude Haiku"]["g_co2e"]["output"] / by["Claude Opus"]["g_co2e"]["output"]
        self.assertAlmostEqual(ratio, 1262 / 5050)

    def test_synthetic_messages_are_not_calls(self):
        self.write("s1.jsonl", [call("m1", "s1", "/Users/me/code/demo"),
                                call("m2", "s1", "/Users/me/code/demo", model="<synthetic>",
                                     inp=0, cw=0, cr=0, out=0)])
        self.assertEqual(self.run_update()["calls"], 1)

    def test_comparison_exact_match_is_singular(self):
        self.assertEqual(tc.format_comparison(4.0), "1 burger with chips")
        self.assertEqual(tc.format_comparison(232.0), "1 economy flight Zurich\u2013London")

    def test_comparison_picks_nearest_on_log_scale(self):
        # 3.1 kg is 0.78 burgers but 6.4 spaghetti portions: the burger is nearer.
        self.assertEqual(tc.format_comparison(3.1), "0.78 burgers with chips")
        self.assertEqual(tc.format_comparison(9000), "1.2 years of an average European's emissions")

    def test_comparison_rotates_through_neighbours_by_day(self):
        days = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"]
        picks = [tc.format_comparison(3.1, d) for d in days]
        self.assertEqual(picks[:3], ["0.78 burgers with chips",
                                     "6.4 portions of spaghetti with tomato sauce",
                                     "0.13 trees' annual CO\u2082 uptake"])
        self.assertEqual(picks[3], picks[0])
        self.assertEqual(tc.format_comparison(3.1, days[1]), picks[1])

    def test_comparison_at_the_ends_of_the_table(self):
        # The smallest item has no neighbour below, so only two items take turns.
        picks = {tc.format_comparison(0.0002, d) for d in ("2026-10-01", "2026-10-02", "2026-10-03")}
        self.assertEqual(picks, {"1 Google search", "0.046 messages sent to ChatGPT"})

    def test_comparisons_are_sorted_and_positive(self):
        kgs = [c["kg"] for c in tc.COMPARISONS]
        self.assertEqual(kgs, sorted(kgs))
        self.assertTrue(all(k > 0 for k in kgs))

    def test_html_embeds_data(self):
        self.write("s1.jsonl", [call("m1", "s1", "/Users/me/code/demo")])
        out = Path(self.tmp.name) / "dash.html"
        tc.write_html(tc.update_history(self.data, self.projects), out)
        html = out.read_text()
        self.assertNotIn("/*DATA*/null", html)
        self.assertIn('"project": "demo"', html)

    def test_branches_are_separate_records(self):
        self.write("s1.jsonl", [call("m1", "s1", "/Users/me/code/demo", branch="main"),
                                call("m2", "s1", "/Users/me/code/demo", branch="feature-x"),
                                call("m3", "s1", "/Users/me/code/demo", branch="feature-x")])
        records = tc.update_history(self.data, self.projects)
        self.assertEqual({r["branch"] for r in records}, {"main", "feature-x"})
        by = tc.tally(records, "branch")
        self.assertEqual(by["feature-x"]["calls"], 2)
        self.assertEqual(by["main"]["calls"], 1)
        self.assertTrue(all(r["cwd"] == "/Users/me/code/demo" for r in records))

    def test_until_bounds_days(self):
        self.write("s1.jsonl", [
            call("m1", "s1", "/Users/me/code/demo", ts="2026-09-29T10:00:00Z"),
            call("m2", "s1", "/Users/me/code/demo", ts="2026-09-30T10:00:00Z"),
            call("m3", "s1", "/Users/me/code/demo", ts="2026-10-01T10:00:00Z")])
        records = tc.update_history(self.data, self.projects)
        self.assertEqual(len(tc.select(records, until="2026-09-30")), 2)
        self.assertEqual(len(tc.select(records, since="2026-09-30", until="2026-09-30")), 1)

    def write_old_history(self, records):
        """A history.json as the version without branches wrote it."""
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "history.json").write_text(json.dumps({"version": 1, "records": records}))

    def old_record(self, session, day, model, calls, cr, project="demo"):
        return {f"{session}|{day}|{model}": {
            "session": session, "day": day, "model": model, "project_dir": self.proj.name,
            "project": project, "input": calls, "cache_write": 10 * calls,
            "cache_read": cr, "output": 5 * calls, "calls": calls}}

    def test_old_keys_migrate_without_double_counting(self):
        # The transcript still exists: its counts move to their branches and
        # the migrated "?" record is left empty.
        self.write("s1.jsonl", [call("m1", "s1", "/Users/me/code/demo", branch="main"),
                                call("m2", "s1", "/Users/me/code/demo", branch="feat")])
        self.write_old_history(self.old_record("s1", "2026-09-30", "claude-opus-5-5", 2, 200))
        records = tc.update_history(self.data, self.projects)
        total = tc.tally(tc.select(records))["total"]
        self.assertEqual(total["calls"], 2)
        self.assertEqual(total["tokens"]["cache_read"], 200)
        stored = json.loads((self.data / "history.json").read_text())["records"]
        self.assertIn("s1|2026-09-30|claude-opus-5-5|?", stored)
        self.assertEqual(stored["s1|2026-09-30|claude-opus-5-5|?"]["calls"], 0)
        self.assertEqual(set(tc.tally(tc.select(records), "branch")), {"main", "feat"})

    def test_migrated_record_keeps_counts_of_deleted_transcripts(self):
        # No transcript left for s0: its old total survives under branch "?".
        self.write("s1.jsonl", [call("m1", "s1", "/Users/me/code/demo")])
        self.write_old_history({**self.old_record("s0", "2026-09-28", "claude-opus-5-5", 3, 300),
                                **self.old_record("s1", "2026-09-30", "claude-opus-5-5", 1, 100)})
        records = tc.update_history(self.data, self.projects)
        by = tc.tally(tc.select(records), "branch")
        self.assertEqual(by["?"]["calls"], 3)
        self.assertEqual(by["main"]["calls"], 1)
        # Running again changes nothing.
        again = tc.tally(tc.select(tc.update_history(self.data, self.projects)), "branch")
        self.assertEqual({k: v["calls"] for k, v in again.items()}, {"?": 3, "main": 1})

    def test_migration_keeps_the_larger_old_total(self):
        # A transcript that lost lines since the old history was written (the
        # old total is larger) keeps the difference under "?".
        self.write("s1.jsonl", [call("m1", "s1", "/Users/me/code/demo")])
        self.write_old_history(self.old_record("s1", "2026-09-30", "claude-opus-5-5", 2, 200))
        total = tc.tally(tc.select(tc.update_history(self.data, self.projects)))["total"]
        self.assertEqual(total["calls"], 2)
        self.assertEqual(total["tokens"]["cache_read"], 200)


if __name__ == "__main__":
    unittest.main()
