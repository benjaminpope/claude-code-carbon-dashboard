"""Tests for the counts export and merge. Run with: python3 -m pytest"""

import json

import pytest

import carbon_counts as cn
import carbon_gemini
import carbon_report

SCOPE = {"roots": ["code"], "groups": {
    "core": ["alpha", "alpha-old"], "tooling": ["tools"]}}


def crec(session, project_dir, model="claude-opus-5", day="2026-10-01", **kw):
    r = {"session": session, "project_dir": project_dir, "model": model, "day": day,
         "input": 1, "cache_write": 10, "cache_read": 100, "output": 1000, "calls": 2,
         "project": "p", "branch": "main", "cwd": "/secret/path"}
    r.update(kw)
    return r


def grec(conv, project, model="gemini-4-carbon", day="2026-10-02", **kw):
    r = {"conversation": conv, "project": project, "model": model, "day": day,
         "input": 5, "cache_read": 50, "output": 500}
    r.update(kw)
    return r


CLAUDE = [
    crec("s1", "-Users-me-code-alpha"),
    crec("s1", "-Users-me-code-alpha", day="2026-10-03", model="claude-sonnet-5-5"),
    crec("s2", "-Users-me-code-alpha--claude-worktrees-feature-x"),
    crec("s3", "-Users-me-code-alpha-old"),
    crec("s4", "-Users-me-code-tools"),
    crec("s5", "-Users-me-code-alpha-science"),   # not alpha: a different repository
    crec("s6", "-Users-me-code-other-alpha"),     # not under a root
    crec("s7", "-Users-me-code-tools", model="<synthetic>"),
    crec("s8", "-Users-me-code-tools", input=0, cache_write=0, cache_read=0, output=0),
]
GEMINI = {f"r{i}": grec(c, p, response=f"r{i}") for i, (c, p) in enumerate(
    [("c1", "owner/alpha"), ("c1", "owner/alpha"), ("c2", "/Users/me/code/tools"),
     ("c3", "owner/elsewhere"), ("c4", "unattributed")])}


def test_gemini_4_carbon_and_hydrogen_classes():
    carbon = carbon_gemini.model_class("gemini-4-carbon-preview")
    hydrogen = carbon_gemini.model_class("gemini-4-hydrogen")
    assert carbon[0][1] == "sonnet" and "unrecognized" not in carbon[1]
    assert hydrogen[0][1] == "haiku" and "unrecognized" not in hydrogen[1]
    assert carbon_gemini.model_class("gemini-4-argon")[0][1] == "opus"
    assert "unrecognized" in carbon_gemini.model_class("gemini-9-new")[1]


def test_scope_matching():
    match = cn.claude_matcher(SCOPE)
    assert match("-Users-me-code-alpha") == "core"
    assert match("-Users-me-code-alpha--claude-worktrees-wt1") == "core"
    assert match("-Users-me-code-alpha-old") == "core"
    assert match("-Users-me-code-tools") == "tooling"
    assert match("-Users-me-code-alpha-science") is None
    assert match("-Users-me-code-other-alpha") is None
    gm = cn.gemini_matcher(SCOPE)
    assert gm("owner/alpha") == "core" and gm("/x/code/tools/") == "tooling"
    assert gm("owner/alpha-science") is None and gm(None) is None


def test_export_totals():
    doc = cn.export_counts(CLAUDE, GEMINI, SCOPE)
    c = doc["counts"]["claude"]
    assert doc["scope_configured"] is True
    assert set(c["groups"]) == {"core", "tooling"}
    core = c["groups"]["core"]
    assert core["total"]["sessions"] == 3                    # s1, s2, s3
    assert set(core["models"]) == {"claude-opus-5", "claude-sonnet-5-5"}
    assert core["models"]["claude-opus-5"]["sessions"] == 3
    assert core["models"]["claude-sonnet-5-5"]["sessions"] == 1
    one = 1 + 10 + 100 + 1000
    assert core["models"]["claude-opus-5"]["total"] == 3 * one
    assert core["models"]["claude-opus-5"]["cache_write"] == 30
    assert core["total"]["total"] == 4 * one
    assert c["combined"]["total"]["sessions"] == 4           # s1 s2 s3 s4
    assert c["combined"]["total"]["total"] == 5 * one
    assert c["out_of_scope"]["total"]["sessions"] == 2        # s5, s6; not <synthetic>, not empty
    assert doc["coverage"]["claude"] == {
        "first_day": "2026-10-01", "last_day": "2026-10-03", "days_with_data": 2}


def test_export_gemini():
    g = cn.export_counts(CLAUDE, GEMINI, SCOPE)["counts"]["gemini"]
    assert g["recorded_components"] == ["input", "cache_read", "output"]
    core = g["groups"]["core"]["models"]["gemini-4-carbon"]
    assert (core["sessions"], core["calls"], core["input"], core["cache_write"]) == (1, 2, 10, 0)
    assert core["total"] == 2 * 555
    assert g["combined"]["total"]["sessions"] == 2
    assert g["out_of_scope"]["total"]["sessions"] == 2


def test_no_scope_file_gives_one_group():
    doc = cn.export_counts(CLAUDE, GEMINI, None)
    assert doc["scope_configured"] is False
    assert list(doc["counts"]["claude"]["groups"]) == ["all"]


def test_no_identifying_content():
    text = json.dumps(cn.export_counts(CLAUDE, GEMINI, SCOPE))
    for needle in ('"s1"', '"c1"', "secret", "/Users", "owner/", "alpha", "elsewhere"):
        assert needle not in text
    agg = json.dumps(cn.export_counts(CLAUDE, GEMINI, SCOPE, aggregate_only=True))
    assert '"sid"' not in agg and "sessions\": [" not in agg
    # session rows are keyed by a hash, never the id itself
    for r in cn.export_counts(CLAUDE, GEMINI, SCOPE)["sessions"]:
        assert len(r["sid"]) == 16 and r["sid"] not in ("s1", "c1")


def test_merge_single_input_matches_export():
    doc = cn.export_counts(CLAUDE, GEMINI, SCOPE)
    merged = cn.merge_exports([doc])
    assert merged["counts"] == doc["counts"]
    assert "sessions" not in merged and merged["machines_merged"] == 1


def test_merge_dedups_by_session_and_takes_max():
    a = cn.export_counts(CLAUDE, GEMINI, SCOPE)
    grown = [dict(r) for r in CLAUDE]
    grown[0]["output"] = 5000                                  # s1 saw more calls on Mac B
    extra = [crec("s9", "-Users-me-code-tools", day="2026-11-05")]
    b = cn.export_counts(grown + extra, GEMINI, SCOPE)
    m = cn.merge_exports([a, b])["counts"]["claude"]
    assert m["combined"]["total"]["sessions"] == 5             # s1-s4 once, plus s9
    opus = m["groups"]["core"]["models"]["claude-opus-5"]
    assert opus["output"] == 5000 + 1000 + 1000
    assert cn.merge_exports([a, b, a])["counts"] == cn.merge_exports([b, a])["counts"]
    cov = cn.merge_exports([a, b])["coverage"]["claude"]
    assert (cov["first_day"], cov["last_day"]) == ("2026-10-01", "2026-11-05")


def test_merge_rejects_aggregate_only_and_empty():
    agg = cn.export_counts(CLAUDE, GEMINI, SCOPE, aggregate_only=True)
    with pytest.raises(ValueError):
        cn.merge_exports([agg])
    with pytest.raises(ValueError):
        cn.merge_exports([])
    with pytest.raises(ValueError):
        cn.merge_exports([{"schema": "other"}])


def test_load_scope(tmp_path, monkeypatch):
    p = tmp_path / "scope.toml"
    p.write_text('roots = ["src"]\n[groups]\ncore = ["a", "b"]\n')
    assert cn.load_scope(p) == {"roots": ["src"], "groups": {"core": ["a", "b"]}}
    assert cn.load_scope(tmp_path / "missing.toml") is None
    monkeypatch.setenv("CLAUDE_CARBON_SCOPE", str(p))
    assert cn.load_scope()["roots"] == ["src"]
    p.write_text("roots = []\n")
    with pytest.raises(SystemExit):
        cn.load_scope(p)


def test_example_scope_parses():
    from pathlib import Path
    scope = cn.load_scope(Path(__file__).with_name("scope.example.toml"))
    assert scope["groups"]


def test_cli_export_and_merge(tmp_path, monkeypatch):
    scope = tmp_path / "scope.toml"
    scope.write_text('[groups]\ncore = ["alpha"]\n')
    monkeypatch.setattr(carbon_report.tc, "update_history", lambda: CLAUDE)
    monkeypatch.setattr(carbon_report.carbon_gemini, "collect", lambda: {"gemini": GEMINI})
    out, merged = tmp_path / "a.json", tmp_path / "m.json"
    carbon_report.main(["--export-counts", str(out), "--scope", str(scope)])
    assert "sessions" in json.loads(out.read_text())
    carbon_report.main(["--merge-counts", str(out), "--out", str(merged)])
    doc = json.loads(merged.read_text())
    assert doc["counts"]["claude"]["groups"]["core"]["total"]["sessions"] == 2
    with pytest.raises(SystemExit):
        carbon_report.main(["--merge-counts", str(out)])
