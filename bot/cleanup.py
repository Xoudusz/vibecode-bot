import subprocess
from pathlib import Path

REPOS_DIR = Path("/repos")


def _run(cmd: list) -> tuple[str, int]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return r.stdout.strip(), r.returncode
    except Exception as e:
        return str(e), 1


def clean_docker() -> str:
    lines = []

    out, _ = _run(["docker", "image", "prune", "-f"])
    lines.append(f"Images: {out or 'nothing removed'}")

    stopped_out, _ = _run(["docker", "ps", "-a", "--filter", "status=exited", "--format", "{{.ID}} {{.Names}}"])
    if stopped_out:
        removed = []
        for line in stopped_out.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            cid, name = parts[0], parts[1]
            _, code = _run(["docker", "rm", cid])
            removed.append(f"{name}({'ok' if code == 0 else 'fail'})")
        lines.append(f"Containers: {', '.join(removed)}")
    else:
        lines.append("Containers: none stopped")

    vol_out, _ = _run(["docker", "volume", "prune", "-f"])
    lines.append(f"Volumes: {vol_out or 'nothing removed'}")

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
