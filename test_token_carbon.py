"""Tests for token_carbon.py. Run with: python3 -m unittest -v"""

import json
import tempfile
import unittest
from pathlib import Path

import token_carbon as tc


def call(mid, session, cwd, ts="2026-09-30T10:00:00Z", model="claude-opus-5-5",
         inp=1, cw=10, cr=100, out=5):
    return {
        "type": "assistant", "sessionId": session, "cwd": cwd, "timestamp": ts,
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

    def test_emissions_use_factors(self):
        self.write("s1.jsonl", [call("m1", "s1", "/Users/me/code/demo",
                                     inp=1_000_000, cw=0, cr=0, out=1_000_000)])
        g = self.run_update()["g_co2e"]
        self.assertAlmostEqual(g["input"], tc.INPUT_G)
        self.assertAlmostEqual(g["output"], tc.OUTPUT_G)

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


if __name__ == "__main__":
    unittest.main()
