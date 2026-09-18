import json
import subprocess
import tempfile
from pathlib import Path

ROOT = {"name": "demo", "private": True, "engines": {"node": "24.18.0", "pnpm": "11.13.0"}}
FILES = {
    "package.json": json.dumps(ROOT, indent="\t") + "\n",
    "pnpm-workspace.yaml": "catalog:\n  vite: 1.0.0\n\npackages:\n  - apps/*\n  - \"packages/*\"\n\nallowBuilds:\n  esbuild: false\n",
    ".npmrc": "@perish:registry=https://git.example/npm/\n@perishlab:registry=https://npm.pkg.github.com/\n",
    "packages/lib/package.json": json.dumps({"name": "@perishlab/lib", "version": "0.0.0"}, indent="\t") + "\n",
    "apps/web/package.json": json.dumps({"name": "@demo/web", "private": True}, indent="\t") + "\n",
    "tools/x/package.json": json.dumps({"name": "@perishlab/outside", "version": "0.0.0"}, indent="\t") + "\n",
    "Containerfile": "FROM scratch\nCOPY demo /demo\n",
    "charts/demo/Chart.yaml": "apiVersion: v2\nname: demo\nversion: 0.0.0\nappVersion: \"0.0.0\"\n",
}


class Repository:
    def __init__(self, files=None):
        self.root = Path(tempfile.mkdtemp())
        self.git("init", "-q")
        for path, body in dict(FILES, **(files or {})).items():
            self.write(path, body)
        self.commit()

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True)

    def write(self, path, body):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", "change")
