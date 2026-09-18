"""Exercise the composite action's shell lifecycle without network services."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize("ready", [True, False])
def test_startup_retains_only_a_ready_process_and_cleans_failures(tmp_path, ready):
    action = Path(__file__).resolve().parents[1] / ".github/actions/setup-biovalidator/action.yml"
    script = textwrap.dedent(action.read_text().split("      run: |\n")[-1])
    executables = {
        "npm": '#!/bin/bash\nif [[ "$1" == root ]]; then echo /fake/modules; else echo biovalidator@main; fi\n',
        "node": f"#!{sys.executable}\n" + textwrap.dedent('''\
            import os
            from pathlib import Path
            import sys
            import time

            if sys.argv[1] == "--version":
                print("v24")
                sys.exit(0)
            # Match npid's exclusive creation: a shell-written file must fail startup.
            time.sleep(0.05)
            with open(os.environ["BIOVALIDATOR_PID_PATH"], "x") as pid_file:
                pid_file.write(str(os.getpid()))
            Path(os.environ["TEST_PID"]).write_text(str(os.getpid()))
            time.sleep(60)
            '''),
        "curl": '#!/bin/bash\n[[ -f "$TEST_PID" ]] || exit 1\nexit ' + ('0' if ready else '1') + '\n',
        "sleep": '#!/bin/bash\nexec /bin/sleep 0.05\n',
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
               BIOVALIDATOR_PID_PATH=str(tmp_path / "server.pid"),
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
