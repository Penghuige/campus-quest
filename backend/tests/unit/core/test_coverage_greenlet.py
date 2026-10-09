"""The coverage gate must attribute greenlet resumptions to their own files.

SQLAlchemy's async bridge switches greenlets inside app coroutine frames.
Run the actual project coverage configuration in an isolated interpreter so
the surrounding pytest collector cannot hide incorrect line attribution.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_project_coverage_preserves_source_lines_across_greenlet_switches(
    tmp_path: Path,
) -> None:
    short = (
        "import asyncio\n"
        "from sqlalchemy.util.concurrency import await_only\n"
        "def run():\n"
        "    before = 'before'\n"
        "    after = await_only(asyncio.sleep(0, result='after'))\n"
        "    return [before, after]\n"
    )
    long = "\n" * 24 + (
        "import asyncio\n"
        "from sqlalchemy.util.concurrency import await_only\n"
        "def run():\n"
        "    before = 'long before'\n"
        "    after = await_only(asyncio.sleep(0, result='long after'))\n"
        "    return [before, after]\n"
    )
    (tmp_path / "short_frame.py").write_text(short, encoding="utf-8")
    (tmp_path / "long_frame.py").write_text(long, encoding="utf-8")
    project_config = Path(__file__).resolve().parents[3] / "pyproject.toml"
    script = """
import json
import sys
import asyncio
from pathlib import Path
import coverage
from sqlalchemy.util.concurrency import greenlet_spawn
import short_frame
import long_frame

collector = coverage.Coverage(
    config_file=sys.argv[1], source=[str(Path.cwd())], data_file=None
)
collector.start()
async def run_both():
    return await asyncio.gather(
        greenlet_spawn(short_frame.run), greenlet_spawn(long_frame.run)
    )
result = asyncio.run(run_both())
collector.stop()
data = collector.get_data()
print(json.dumps({
    'result': result,
    'short': sorted(data.lines(str(Path(short_frame.__file__).resolve())) or []),
    'long': sorted(data.lines(str(Path(long_frame.__file__).resolve())) or []),
}))
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(project_config)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    observed = json.loads(child.stdout)
    assert observed["result"] == [["before", "after"], ["long before", "long after"]]
    assert observed["short"] == [4, 5, 6]
    assert observed["long"] == [28, 29, 30]
