import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def repository_env_read_calls(path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        is_read = isinstance(function, ast.Attribute) and function.attr in {"read_text", "read_bytes", "open"}
        is_builtin_open = isinstance(function, ast.Name) and function.id == "open"
        if not (is_read or is_builtin_open):
            continue
        segment = ast.get_source_segment(source, node) or ""
        if ".env" in segment:
            findings.append(f"{path.name}:{node.lineno}")
    return findings


class NoRepositoryEnvFileReadsTests(unittest.TestCase):
    def test_unittest_modules_do_not_read_repository_env_files(self):
        findings = []
        for path in sorted((PROJECT_ROOT / "tests").glob("test_*.py")):
            if path == Path(__file__):
                continue
            findings.extend(repository_env_read_calls(path))
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
