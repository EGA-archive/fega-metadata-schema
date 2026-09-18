"""Exercise the composite action's shell lifecycle without network services."""
import os
from pathlib import Path
import signal
import subprocess
import textwrap

import pytest


@pytest.mark.parametrize("ready", [True, False])
def test_startup_retains_only_a_ready_process_and_cleans_failures(tmp_path, ready):
    action = Path(__file__).resolve().parents[1] / ".github/actions/setup-biovalidator/action.yml"
    script = textwrap.dedent(action.read_text().split("      run: |\n")[-1])
    executables = {
        "npm": '#!/bin/bash\nif [[ "$1" == root ]]; then echo /fake/modules; else echo biovalidator@main; fi\n',
        "node": '#!/bin/bash\nif [[ "$1" == --version ]]; then echo v24; exit; fi\nprintf "%s" "$$" > "$TEST_PID"\nexec /bin/sleep 60\n',
        "curl": '#!/bin/bash\nexit ' + ('0' if ready else '1') + '\n',
        "sleep": '#!/bin/bash\nexit 0\n',
    }
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, content in executables.items():
        executable = bin_dir / name
        executable.write_text(content)
        executable.chmod(0o755)
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
               BIOVALIDATOR_RATE_LIMIT_MAX="5000", BIOVALIDATOR_PORT="3020",
               BIOVALIDATOR_LOG_FILE=str(tmp_path / "server.log"),
               BIOVALIDATOR_PID_FILE=str(tmp_path / "server.pid"), TEST_PID=str(tmp_path / "child.pid"))
    child = None
    try:
        result = subprocess.run(["bash", "-eo", "pipefail", "-c", script], env=env,
                                capture_output=True, text=True, timeout=10)
        marker = tmp_path / "child.pid"
        if marker.exists():
            child = int(marker.read_text())
        assert (result.returncode == 0) is ready
        assert (tmp_path / "server.pid").exists() is ready
        if ready:
            child = int((tmp_path / "server.pid").read_text())
            os.kill(child, 0)
        elif child:
            with pytest.raises(ProcessLookupError):
                os.kill(child, 0)
    finally:
        if child:
            try:
                os.kill(child, signal.SIGTERM)
            except ProcessLookupError:
                pass
