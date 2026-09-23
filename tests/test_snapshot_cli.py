"""Exercise the real artifact chain and its repository output boundary."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jev_git_graph.cli import parser, run
from jev_git_graph.errors import JgError
from test_snapshot import make_repo, git


class SnapshotCliTests(unittest.TestCase):
    def test_python_limitations_and_aliases_reach_groups(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as tmp:
            root = Path(tmp)
            repo, _, _, _ = make_repo(root)
            git(repo, 'switch', 'topic')
            (repo / 'broken.py').write_text('def broken(:\n')
            (repo / ':(glob)x.py').write_text('VALUE = 1\n')
            git(repo, 'add', '.')
            git(repo, 'commit', '-qm', 'unparsed Python')
            git(repo, 'branch', 'topic-alias')
            git(repo, 'switch', 'main')
            def call(*args):
                return Path(run(parser().parse_args(list(args))))
            inv = call('inventory', '--repo', str(repo), '--out', str(root / 'inventory'))
            with patch('jev_git_graph.snapshot._activity', return_value=1):
                snap = call('snapshot', '--repo', str(repo), '--inventory', str(inv), '--out', str(root / 'snapshot'))
            contributions = call('contributions', '--repo', str(repo), '--snapshot', str(snap), '--out', str(root / 'contributions.json'))
            groups = call('groups', '--repo', str(repo), '--contributions', str(contributions), '--out', str(root / 'groups'))
            artifact = json.loads(groups.read_text())
            self.assertEqual(artifact['coverage']['grouped_source_units'], 6)
            self.assertTrue(all(not group['context_complete'] for group in artifact['groups']))

    def test_chain_uses_fixed_objects_and_writes_no_source_files(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as tmp:
            root = Path(tmp)
            repo, _, _, _ = make_repo(root)
            def call(*args):
                return Path(run(parser().parse_args(list(args))))
            inv = call('inventory', '--repo', str(repo), '--out', str(root / 'inventory'))
            before = git(repo, 'status', '--porcelain=v1')
            with patch('jev_git_graph.snapshot._activity', return_value=1):
                snap = call('snapshot', '--repo', str(repo), '--inventory', str(inv), '--out', str(root / 'snapshot'))
            # Moving main must not change the pinned advisory analysis.
            (repo / 'advanced.txt').write_text('new main\n')
            git(repo, 'add', '.')
            git(repo, 'commit', '-qm', 'advance main')
            contributions = call('contributions', '--repo', str(repo), '--snapshot', str(snap), '--out', str(root / 'contributions.json'))
            groups = call('groups', '--repo', str(repo), '--contributions', str(contributions), '--out', str(root / 'groups'))
            artifact = json.loads(groups.read_text())
            self.assertEqual(artifact['coverage']['grouped_source_units'], 1)
            self.assertEqual(before, git(repo, 'status', '--porcelain=v1'))
            for command, flag, source in [('contributions', '--snapshot', snap), ('groups', '--contributions', contributions)]:
                with self.assertRaises(JgError):
                    call(command, '--repo', str(repo), flag, str(source), '--out', str(repo / '.git' / 'report'))


if __name__ == '__main__':
    unittest.main()
