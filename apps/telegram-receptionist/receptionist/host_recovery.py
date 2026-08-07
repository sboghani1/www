from __future__ import annotations

import json
import subprocess
import sys

NSENTER = ["/usr/bin/nsenter", "--mount=/proc/1/ns/mnt", "--"]
WRITE_PATHS = ("/opt", "/etc", "/usr/local")
FILESYSTEM_ERROR_MARKERS = (
    "ext4-fs error",
    "i/o error",
    "buffer i/o error",
    "remounting filesystem read-only",
    "aborting journal",
    "journal has aborted",
)


def run(
    command: list[str], *, check: bool = False
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        capture_output=True,
        text=True,
        timeout=30,
    )


def run_host(
    command: list[str], *, check: bool = False
) -> subprocess.CompletedProcess[str]:
    return run([*NSENTER, *command], check=check)


def mount_options(*, host: bool) -> str:
    command = ["/usr/bin/findmnt", "-n", "-o", "OPTIONS", "/"]
    result = run_host(command, check=True) if host else run(command, check=True)
    return result.stdout.strip()


def mount_is_read_only(options: str) -> bool:
    return "ro" in options.split(",")


def filesystem_error_lines(logs: str) -> list[str]:
    return [
        line
        for line in logs.splitlines()
        if any(marker in line.lower() for marker in FILESYSTEM_ERROR_MARKERS)
    ]


def kernel_filesystem_errors() -> list[str]:
    result = run(
        ["/usr/bin/journalctl", "-k", "-b", "--no-pager"],
        check=False,
    )
    return filesystem_error_lines(result.stdout + result.stderr)


def host_write_probe(directory: str) -> bool:
    result = run_host(
        [
            "/usr/bin/mktemp",
            f"{directory}/.receptionist-host-diagnostic.XXXXXX",
        ],
        check=False,
    )
    if result.returncode != 0:
        return False
    temporary = result.stdout.strip()
    if not temporary:
        return False
    run_host(["/usr/bin/rm", "-f", temporary], check=False)
    return True


def diagnostics() -> dict[str, object]:
    errors = kernel_filesystem_errors()
    return {
        "caller_root_options": mount_options(host=False),
        "host_root_options": mount_options(host=True),
        "host_write_paths": {
            path: host_write_probe(path) for path in WRITE_PATHS
        },
        "kernel_filesystem_errors": errors[-20:],
        "note": (
            "errors=remount-ro is a configured safety policy, not evidence "
            "that an error occurred. Check the leading ro/rw option and "
            "kernel_filesystem_errors."
        ),
    }


def remount_root_read_write() -> None:
    options = mount_options(host=True)
    if not mount_is_read_only(options):
        print(json.dumps({"status": "already_read_write", **diagnostics()}))
        return
    errors = kernel_filesystem_errors()
    if errors:
        print(
            json.dumps(
                {
                    "status": "refused",
                    "reason": (
                        "Kernel filesystem errors are present; remounting "
                        "could worsen corruption. Use provider recovery and "
                        "offline fsck."
                    ),
                    "kernel_filesystem_errors": errors[-20:],
                }
            )
        )
        raise SystemExit(2)
    run_host(
        ["/usr/bin/mount", "-o", "remount,rw", "/"],
        check=True,
    )
    result = diagnostics()
    if mount_is_read_only(str(result["host_root_options"])) or not all(
        result["host_write_paths"].values()
    ):
        raise SystemExit("Root remount did not restore required write paths.")
    print(json.dumps({"status": "remounted_read_write", **result}))


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: receptionist-host-recovery ACTION")
    action = sys.argv[1]
    if action == "diagnose":
        print(json.dumps(diagnostics()))
        return
    if action == "remount-root-rw":
        remount_root_read_write()
        return
    raise SystemExit("Unsupported host recovery action.")


if __name__ == "__main__":
    main()
