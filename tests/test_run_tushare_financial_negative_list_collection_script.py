from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path


def test_script_checks_authorization_before_keychain() -> None:
    script = Path("scripts/run_tushare_financial_negative_list_collection.sh").read_text(encoding="utf-8")
    verify_idx = script.index("verify-financial-negative-list-collection-run-contract")
    security_idx = script.index("security find-generic-password")
    assert verify_idx < security_idx


def test_script_does_not_put_token_on_command_line() -> None:
    script = Path("scripts/run_tushare_financial_negative_list_collection.sh").read_text(encoding="utf-8")
    assert "--token" not in script
    assert "AIQ_TUSHARE_TOKEN=" not in script.split("exec caffeinate -i", maxsplit=1)[1]


def test_script_uses_required_authorization_env_var() -> None:
    script = Path("scripts/run_tushare_financial_negative_list_collection.sh").read_text(encoding="utf-8")
    assert "AIQ_E11B_COLLECTION_AUTHORIZATION_FILE" in script
    assert "--require-authorized" in script


def test_script_relative_authorization_path_is_consistent_and_security_not_called_when_verify_fails(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    scripts_dir = project / "scripts"
    venv_bin = project / ".venv" / "bin"
    sandbox_cwd = tmp_path / "cwd"
    scripts_dir.mkdir(parents=True)
    venv_bin.mkdir(parents=True)
    sandbox_cwd.mkdir(parents=True)
    (project / "config" / "research").mkdir(parents=True)
    (project / "config" / "research" / "auth.json").write_text("{}", encoding="utf-8")

    source_script = Path("scripts/run_tushare_financial_negative_list_collection.sh").read_text(encoding="utf-8")
    script_path = scripts_dir / "run_tushare_financial_negative_list_collection.sh"
    script_path.write_text(source_script, encoding="utf-8")
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR)

    python_log = project / "python.log"
    fake_python = venv_bin / "python"
    fake_python.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                'printf \'%s\\n\' "$*" >> "$PYTHON_LOG"',
                'printf \'PYTHONPATH=%s\\n\' "$PYTHONPATH" >> "$PYTHON_LOG"',
                (
                    'if [[ "$1" == "-m" && "$2" == "app.cli" '
                    '&& "$3" == "verify-financial-negative-list-collection-run-contract" ]]; then'
                ),
                "  for ((i=1;i<=$#;i++)); do",
                '    if [[ "${!i}" == "--authorization-file" ]]; then',
                "      j=$((i+1))",
                '      printf \'AUTH=%s\\n\' "${!j}" >> "$PYTHON_LOG"',
                "      break",
                "    fi",
                "  done",
                "  exit 23",
                "fi",
                "exit 24",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)

    fake_security_dir = tmp_path / "bin"
    fake_security_dir.mkdir()
    fake_security = fake_security_dir / "security"
    fake_security.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\necho security-called > "$SECURITY_MARKER"\nexit 0\n',
        encoding="utf-8",
    )
    fake_security.chmod(fake_security.stat().st_mode | stat.S_IXUSR)

    env = dict(os.environ)
    env["AIQ_E11B_COLLECTION_AUTHORIZATION_FILE"] = "config/research/auth.json"
    env["PYTHON_LOG"] = str(python_log)
    env["SECURITY_MARKER"] = str(project / "security.marker")
    env["PYTHONPATH"] = "existing-pythonpath"
    env["PATH"] = str(fake_security_dir) + os.pathsep + env.get("PATH", "")
    proc = subprocess.run(
        ["bash", str(script_path)],
        cwd=sandbox_cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 23
    log_lines = python_log.read_text(encoding="utf-8").splitlines()
    auth_line = next(line for line in log_lines if line.startswith("AUTH="))
    assert auth_line == f"AUTH={project / 'config' / 'research' / 'auth.json'}"
    py_path_line = next(line for line in log_lines if line.startswith("PYTHONPATH="))
    assert py_path_line == f"PYTHONPATH={project / 'src'}:existing-pythonpath"
    assert not (project / "security.marker").exists()


def test_script_sets_project_src_pythonpath_for_verify_and_collect_invocations(tmp_path: Path) -> None:
    project = tmp_path / "project"
    scripts_dir = project / "scripts"
    venv_bin = project / ".venv" / "bin"
    sandbox_cwd = tmp_path / "cwd"
    scripts_dir.mkdir(parents=True)
    venv_bin.mkdir(parents=True)
    sandbox_cwd.mkdir(parents=True)
    (project / "config" / "research").mkdir(parents=True)
    (project / "config" / "research" / "auth.json").write_text("{}", encoding="utf-8")

    source_script = Path("scripts/run_tushare_financial_negative_list_collection.sh").read_text(encoding="utf-8")
    script_path = scripts_dir / "run_tushare_financial_negative_list_collection.sh"
    script_path.write_text(source_script, encoding="utf-8")
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR)

    python_log = project / "python.log"
    fake_python = venv_bin / "python"
    fake_python.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                'printf \'CMD=%s\\n\' "$*" >> "$PYTHON_LOG"',
                'printf \'PYTHONPATH=%s\\n\' "$PYTHONPATH" >> "$PYTHON_LOG"',
                (
                    'if [[ "$1" == "-m" && "$2" == "app.cli" '
                    '&& "$3" == "verify-financial-negative-list-collection-run-contract" ]]; then'
                ),
                "  exit 0",
                "fi",
                (
                    'if [[ "$1" == "-m" && "$2" == "app.cli" '
                    '&& "$3" == "collect-tushare-financial-negative-list" ]]; then'
                ),
                "  exit 37",
                "fi",
                "exit 24",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_security = fake_bin / "security"
    fake_security.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nprintf 'mock-token\\n'\n",
        encoding="utf-8",
    )
    fake_security.chmod(fake_security.stat().st_mode | stat.S_IXUSR)

    fake_caffeinate = fake_bin / "caffeinate"
    fake_caffeinate.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\nif [[ "${1:-}" == "-i" ]]; then\n  shift\nfi\nexec "$@"\n',
        encoding="utf-8",
    )
    fake_caffeinate.chmod(fake_caffeinate.stat().st_mode | stat.S_IXUSR)

    env = dict(os.environ)
    env["AIQ_E11B_COLLECTION_AUTHORIZATION_FILE"] = "config/research/auth.json"
    env["PYTHON_LOG"] = str(python_log)
    env["PYTHONPATH"] = "existing-pythonpath"
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    proc = subprocess.run(
        ["bash", str(script_path)],
        cwd=sandbox_cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 37
    log_lines = python_log.read_text(encoding="utf-8").splitlines()
    command_lines = [line for line in log_lines if line.startswith("CMD=")]
    pythonpath_lines = [line for line in log_lines if line.startswith("PYTHONPATH=")]
    assert len(command_lines) == 2
    assert len(pythonpath_lines) == 2
    assert "verify-financial-negative-list-collection-run-contract" in command_lines[0]
    assert "collect-tushare-financial-negative-list" in command_lines[1]
    expected_path = f"PYTHONPATH={project / 'src'}:existing-pythonpath"
    assert pythonpath_lines[0] == expected_path
    assert pythonpath_lines[1] == expected_path
