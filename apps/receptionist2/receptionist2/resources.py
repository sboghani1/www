from __future__ import annotations

from pathlib import Path

PROC = Path("/proc")


def meminfo(proc: Path = PROC) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in (proc / "meminfo").read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            values[parts[0].rstrip(":")] = int(parts[1])
    swap_total = values.get("SwapTotal", 0)
    return {
        "mem_total_mb": values.get("MemTotal", 0) // 1024,
        "mem_avail_mb": values.get("MemAvailable", 0) // 1024,
        "swap_total_mb": swap_total // 1024,
        "swap_used_mb": (swap_total - values.get("SwapFree", 0)) // 1024,
    }


def process_group_rss_mb(process_group_id: int, proc: Path = PROC) -> float | None:
    total_kb = 0
    found = False
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            fields = stat.rsplit(")", 1)[1].split()
            if int(fields[2]) != process_group_id:
                continue
            found = True
            for line in (entry / "status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    total_kb += int(line.split()[1])
                    break
        except (OSError, IndexError, ValueError):
            continue
    return total_kb / 1024 if found else None


def resource_summary(process_group_id: int | None, proc: Path = PROC) -> str:
    try:
        memory = meminfo(proc)
        load = (proc / "loadavg").read_text().split()[0]
    except OSError:
        return "Resources: unavailable"
    rss = (
        process_group_rss_mb(process_group_id, proc)
        if process_group_id
        else None
    )
    agent = (
        f"Agent: {rss:.0f}MB RSS"
        if rss is not None
        else "Agent: process metrics unavailable"
    )
    return (
        f"{agent}\n"
        f"VPS: {memory['mem_avail_mb']}MB of {memory['mem_total_mb']}MB RAM "
        f"free · swap {memory['swap_used_mb']}/{memory['swap_total_mb']}MB · "
        f"load {load}"
    )
