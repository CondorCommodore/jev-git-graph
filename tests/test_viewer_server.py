import json
import tempfile
import unittest
from pathlib import Path

from jev_git_graph.viewer import bootstrap


class ViewerServerTests(unittest.TestCase):
    def test_bootstrap_has_documents_and_never_exposes_source_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "inventory.json"
            path.write_text(json.dumps({"kind": "inventory", "branch": "</script>"}), encoding="utf-8")
            document = bootstrap({"inventory": path}).decode("utf-8")
            self.assertIn('"name":"inventory.json"', document)
            self.assertIn('"kind":"inventory"', document)
            self.assertNotIn(str(root), document)
            self.assertNotIn("</script>", document)


if __name__ == "__main__":
    unittest.main()
