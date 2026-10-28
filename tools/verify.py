"""Build the native engine and verify its bridges without provider calls."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]


def executable(value):
    resolved = shutil.which(value) or value
    path = Path(resolved).expanduser().absolute()
    if not path.is_file():
        raise ValueError(f"Not an executable file: {value}")
    return str(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cargo", default="cargo")
    parser.add_argument("--node", default="node")
    parser.add_argument("--output", type=Path, default=ROOT / "output/verification")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {"started_at_utc": datetime.now(timezone.utc).isoformat(),
              "passed": False, "platform": platform.platform(),
              "python": platform.python_version(), "steps": [],
              "scope": "Native engine and integration checks; no speech-quality claim"}
    started = perf_counter()
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.setdefault("CARGO_BUILD_JOBS", "2")

    def run(name, command, timeout=900):
        before = perf_counter()
        log = output / (name + ".log")
        with log.open("w", encoding="utf-8") as stream:
            result = subprocess.run([str(part) for part in command], cwd=ROOT,
                                    env=environment, stdout=stream,
                                    stderr=subprocess.STDOUT, timeout=timeout, check=False)
        passed = result.returncode == 0
        report["steps"].append({"name": name, "passed": passed,
                                "seconds": round(perf_counter() - before, 3),
                                "log": log.name})
        print(("PASS " if passed else "FAIL ") + name, flush=True)
        if not passed:
            print(log.read_text(encoding="utf-8", errors="replace")[-12000:], file=sys.stderr)
            raise RuntimeError(f"Check failed; inspect {log.name}")

    try:
        if environment.get("CARGO_BUILD_TARGET"):
            raise ValueError("Verification requires a native build; unset CARGO_BUILD_TARGET")
        cargo = executable(args.cargo)
        node = executable(args.node)
        run("rust-format", [cargo, "fmt", "--", "--check"])
        run("rust-build", [cargo, "build", "--release", "--locked", "--target-dir", ROOT / "target"])
        run("rust-tests", [cargo, "test", "--release", "--locked", "--target-dir", ROOT / "target",
                           "--", "--include-ignored"])
        extension = ".exe" if sys.platform == "win32" else ""
        binary = ROOT / "target/release" / ("sao-denoise" + extension)
        if not binary.is_file():
            raise ValueError("Release binary is missing")
        environment["SAO_DENOISE_EXE"] = str(binary)
        for suite in ("python", "lab"):
            run(suite, [sys.executable, ROOT / "tools/run_tests.py", suite], timeout=180)
        run("browser", [node, "--test", "test-app/test-recorder.cjs", "test-app/test-playback.cjs",
                              "test-app/test-live.cjs"], timeout=60)
        report["passed"] = True
    except (ImportError, ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        report["error"] = str(error)
        print("Verification failed: " + str(error), file=sys.stderr)
    finally:
        report["elapsed_seconds"] = round(perf_counter() - started, 3)
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print("Report: " + str(output / "report.json"))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
