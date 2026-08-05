import subprocess
from pathlib import Path

import docker as docker_sdk

REPOS_DIR = Path("/repos")


def _run(cmd: list) -> tuple[str, int]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return r.stdout.strip(), r.returncode
    except Exception as e:
        return str(e), 1


def clean_docker() -> str:
    lines = []
    try:
        client = docker_sdk.from_env()

        # Dangling images
        pruned = client.images.prune(filters={"dangling": True})
        reclaimed = pruned.get("SpaceReclaimed", 0)
        deleted = len(pruned.get("ImagesDeleted") or [])
        lines.append(f"Images: removed {deleted} ({reclaimed // 1024 // 1024}MB reclaimed)")

        # Stopped containers
        stopped = client.containers.list(filters={"status": "exited"})
        removed = []
        for c in stopped:
            try:
                name = c.name
                c.remove()
                removed.append(f"{name}(ok)")
            except Exception as e:
                removed.append(f"{c.name}(fail:{e})")
        lines.append(f"Containers: {', '.join(removed) if removed else 'none stopped'}")

        # Unused volumes
        pruned_v = client.volumes.prune()
        vols = len(pruned_v.get("VolumesDeleted") or [])
        lines.append(f"Volumes: removed {vols}")

        client.close()
    except Exception as e:
        lines.append(f"Docker error: {e}")

    return "\n".join(lines)


def clean_git() -> str:
    if not REPOS_DIR.exists():
        return "Repos dir not mounted"

    results = []
    for repo in sorted(REPOS_DIR.iterdir()):
        if not (repo / ".git").exists():
            continue
        _run(["git", "-C", str(repo), "fetch", "--prune", "-q"])
        out, _ = _run(["git", "-C", str(repo), "branch", "-vv"])
        for line in out.splitlines():
            if ": gone]" not in line:
                continue
            branch = line.strip().lstrip("* ").split()[0]
            _, code = _run(["git", "-C", str(repo), "branch", "-d", branch])
            if code != 0:
                _, code = _run(["git", "-C", str(repo), "branch", "-D", branch])
            results.append(f"{repo.name}/{branch}: {'deleted' if code == 0 else 'failed'}")

    return "\n".join(results) if results else "No gone branches"


HANDLERS: dict[str, callable] = {
    "approve_docker": clean_docker,
    "approve_git": clean_git,
}
