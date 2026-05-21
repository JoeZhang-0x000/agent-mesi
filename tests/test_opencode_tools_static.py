from __future__ import annotations

from pathlib import Path
import json


def test_opencode_tools_are_present_and_call_python():
    root = Path(".opencode/tools")
    expected = {
        "read.ts",
        "write.ts",
        "edit.ts",
        "bash.ts",
        "patch.ts",
        "apply_patch.ts",
        "mesi_status.ts",
        "mesi_refresh.ts",
    }
    assert {path.name for path in root.glob("*.ts")} == expected

    for name in expected:
        content = (root / name).read_text(encoding="utf-8")
        assert '@opencode-ai/plugin' in content
        assert 'export default tool' in content
        assert 'MESI_PYTHON' in content
        assert 'python -m mesi_runtime' not in content
        assert '-m' in content and 'mesi_runtime' in content


def test_opencode_plugin_dependency_declared():
    package = json.loads(Path("package.json").read_text(encoding="utf-8"))
    assert package["devDependencies"]["@opencode-ai/plugin"] == "1.14.39"
