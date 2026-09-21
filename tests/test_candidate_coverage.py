import unittest
import tempfile
from pathlib import Path

from jev_git_graph.candidates import select_with_coverage, add_ancestry_evidence
from jev_git_graph.git import GitRunner


class CoverageTests(unittest.TestCase):
    def test_ancestry_direction_is_measured_on_real_commits(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = GitRunner(Path(directory))
            runner.run("init", "--quiet")
            def commit():
                runner.run("-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid", "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "synthetic")
                return runner.run("rev-parse", "HEAD").strip()
            ancestor = commit()
            descendant = commit()
            candidates = [{"endpoints": {"a": {"tip": ancestor}, "b": {"tip": descendant}}, "evidence": {}}]
            add_ancestry_evidence(runner, candidates)
            evidence = candidates[0]["evidence"]
            self.assertTrue(evidence["a_ancestor_of_b"])
            self.assertFalse(evidence["b_ancestor_of_a"])
            self.assertEqual(evidence["a_commits_not_in_b"], 0)
            self.assertEqual(evidence["b_commits_not_in_a"], 1)

    def test_dense_group_does_not_consume_entire_budget(self):
        def pair(identifier, a, b):
            return {"id": identifier, "endpoints": {"a": {"branch": a}, "b": {"branch": b}}}
        ranked = [pair("1", "a", "b"), pair("2", "a", "c"), pair("3", "b", "c"), pair("4", "d", "e")]
        self.assertEqual([item["id"] for item in select_with_coverage(ranked, 3)], ["1", "2", "4"])
        self.assertEqual(len(select_with_coverage(ranked, 10)), 4)
        self.assertEqual(len(select_with_coverage(ranked, 1)), 1)
