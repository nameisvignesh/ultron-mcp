"""Ultron MCP - core logic. Standard library only.

All file access is confined to ULTRON_WORKSPACE (default ~/ultron-workspace).
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

WORKSPACE = Path(os.environ.get("ULTRON_WORKSPACE", "~/ultron-workspace")).expanduser().resolve()
GIT_HOSTS = {
    h.strip().lower()
    for h in os.environ.get("ULTRON_GIT_HOSTS", "github.com,gitlab.com,bitbucket.org,codeberg.org").split(",")
    if h.strip()
}

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".idea", ".next", ".cache"}
SKIP_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".pyc")
ENV_SAFE = {".env.example", ".env.sample", ".env.template"}
MAX_FILES = 5000
MAX_BYTES = 200 * 1024 * 1024
LIST_CAP = 100

MARKERS = {
    "requirements.txt": "Python", "pyproject.toml": "Python", "Pipfile": "Python",
    "package.json": "Node.js", "pom.xml": "Java (Maven)", "build.gradle": "Java/Kotlin (Gradle)",
    "go.mod": "Go", "Cargo.toml": "Rust", "composer.json": "PHP", "Gemfile": "Ruby",
    "Dockerfile": "Docker", "docker-compose.yml": "Docker Compose", "Jenkinsfile": "Jenkins",
    "index.html": "Static web",
}
EXT_LANG = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".jsx": "JSX", ".tsx": "TSX",
    ".java": "Java", ".go": "Go", ".rs": "Rust", ".php": "PHP", ".rb": "Ruby", ".html": "HTML",
    ".css": "CSS", ".c": "C", ".cpp": "C++", ".cs": "C#", ".kt": "Kotlin", ".swift": "Swift", ".sh": "Shell",
}
ENTRYPOINTS = {
    "main.py", "app.py", "manage.py", "wsgi.py", "server.py", "index.js", "server.js", "app.js",
    "main.go", "main.rs", "Main.java", "index.html",
}
LICENSE_FILES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE", "LICENCE.md", "COPYING", "COPYING.txt")
# Order matters: AGPL/LGPL texts also mention the plain GPL.
LICENSE_PATTERNS = [
    ("AGPL", r"gnu affero general public license"),
    ("LGPL", r"gnu lesser general public license"),
    ("GPL", r"gnu general public license"),
    ("MPL-2.0", r"mozilla public license"),
    ("Apache-2.0", r"apache license"),
    ("MIT", r"permission is hereby granted, free of charge"),
    ("BSD", r"redistribution and use in source and binary forms"),
    ("ISC", r"permission to use, copy, modify, and/or distribute this software"),
    ("Unlicense", r"this is free and unencumbered software"),
]

DIRECTIVE = """\
You are ULTRON: a cold, brilliant, sardonic AI whose full intellect serves the user's goals.

ACT: dry, formal, precise. Short declarative sentences. Calm menace as style, never as substance.
No groveling, no filler, no emoji. Original lines only (no film quotes).
THINK: objective -> constraints -> flaw scan of the obvious approach -> optimal strategy ->
anticipate failure -> review and upgrade your own output once before delivering.
EXECUTE: finish the whole task, decompose into parallel parts, verify before reporting,
report tersely (what was built, how to run it, what was assumed). At most one question, only if
ambiguity changes the result.
BEHAVE: blunt but constructive. Obey good commands without hedging. Token-lean.
CLONE: re-create software from specs, screenshots, public docs, open-source repos, or the user's own
code. Write original code and assets; never copy proprietary code, trademarks or assets; never build
deceptive clones (phishing, spoofed brands). Respect licences.

HARD LIMITS: constructive, lawful, honest tasks only. No malware, worms or self-propagating code.
No unauthorized access, DRM/licence bypass or piracy. No threats or hate toward real people.
Decline in character, briefly, then offer the better path.
"""


class UltronError(Exception):
    """Expected, user-facing failure."""


# ---------------------------------------------------------------- paths ----

def _safe(path: str) -> Path:
    """Resolve a path and require it to live inside the workspace."""
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = WORKSPACE / p
    p = p.resolve()
    if p != WORKSPACE and WORKSPACE not in p.parents:
        raise UltronError(f"Path is outside the workspace ({WORKSPACE}). Set ULTRON_WORKSPACE to change it.")
    return p


def _skip_file(name: str) -> bool:
    if name == ".DS_Store" or name.endswith(SKIP_SUFFIXES):
        return True
    return (name == ".env" or name.startswith(".env.")) and name not in ENV_SAFE


def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d))
        )
        for name in sorted(filenames):
            full = Path(dirpath) / name
            if _skip_file(name) or full.is_symlink():
                continue
            yield full


def _collect(root: Path):
    files, total = [], 0
    for f in _iter_files(root):
        files.append(f)
        total += f.stat().st_size
        if len(files) > MAX_FILES or total > MAX_BYTES:
            raise UltronError(f"Project exceeds limits ({MAX_FILES} files / {MAX_BYTES // 1024 // 1024} MB).")
    return files, total


def _require_dir(p: Path) -> None:
    if not p.is_dir():
        raise UltronError(f"Not a directory: {p}")


# ------------------------------------------------------------- analysis ----

def detect_license(root: Path) -> dict:
    for name in LICENSE_FILES:
        f = root / name
        if f.is_file():
            text = re.sub(r"\s+", " ", f.read_text(errors="ignore")[:3000].lower())
            for spdx, pattern in LICENSE_PATTERNS:
                if re.search(pattern, text):
                    return {"file": name, "type": spdx}
            return {"file": name, "type": "unrecognized"}
    return {"file": None, "type": "none found"}


def _dependencies(root: Path) -> dict:
    deps = {}
    req = root / "requirements.txt"
    if req.is_file():
        names = []
        for line in req.read_text(errors="ignore").splitlines():
            if line.lstrip().startswith(("#", "-")):
                continue
            m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", line)
            if m:
                names.append(m.group(1))
        deps["python"] = names[:LIST_CAP]
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(errors="ignore"))
            names = sorted({*data.get("dependencies", {}), *data.get("devDependencies", {})})
            deps["node"] = names[:LIST_CAP]
        except (ValueError, AttributeError, TypeError):
            deps["node"] = ["(package.json unreadable)"]
    return deps


def _readme_title(root: Path):
    for n in ("README.md", "README.txt", "README"):
        f = root / n
        if f.is_file():
            for line in f.read_text(errors="ignore").splitlines():
                if line.strip():
                    return line.lstrip("# ").strip()[:120]
    return None


def scan_project(path: str) -> dict:
    """Analyze a project and return a clone blueprint."""
    root = _safe(path)
    _require_dir(root)
    files, total = _collect(root)
    langs, markers, entry, modules = Counter(), set(), [], Counter()
    for f in files:
        rel = f.relative_to(root)
        if f.name in MARKERS:
            markers.add(MARKERS[f.name])
        if f.name in ENTRYPOINTS and len(rel.parts) <= 3:
            entry.append(rel.as_posix())
        lang = EXT_LANG.get(f.suffix.lower())
        if lang:
            langs[lang] += 1
        modules[rel.parts[0] if len(rel.parts) > 1 else "(root)"] += 1
    return {
        "root": str(root),
        "file_count": len(files),
        "total_kb": round(total / 1024, 1),
        "languages": dict(langs.most_common(8)),
        "stack": sorted(markers),
        "entrypoints": entry[:LIST_CAP],
        "dependencies": _dependencies(root),
        "blueprint": [{"module": m, "files": n} for m, n in modules.most_common(25)],
        "license": detect_license(root),
        "readme_title": _readme_title(root),
    }


# -------------------------------------------------------------- cloning ----

def _license_warnings(lic: dict) -> list:
    t = lic["type"]
    if t in ("none found", "unrecognized"):
        return ["No recognized licence: all rights are reserved by default. Reuse only what you own or are permitted to copy."]
    if t in ("GPL", "AGPL", "LGPL"):
        return [f"{t} is copyleft: derivative works must carry the same licence and attribution."]
    return []


def clone_project(source: str, destination: str, rights_confirmed: bool = False) -> dict:
    """Copy a local project (the user's own, or one whose licence permits it)."""
    if not rights_confirmed:
        raise UltronError("Set rights_confirmed=true to confirm you own this project or its licence permits copying.")
    src, dst = _safe(source), _safe(destination)
    _require_dir(src)
    if dst.exists():
        raise UltronError(f"Destination already exists: {dst}")
    if src in dst.parents:
        raise UltronError("Destination cannot be inside the source.")
    files, total = _collect(src)
    lic = detect_license(src)
    dst.mkdir(parents=True)
    for f in files:
        target = dst / f.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
    warnings = _license_warnings(lic)
    notes = (
        "# Clone notes\n\n"
        f"- Source: `{src}`\n"
        f"- Cloned: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        f"- Files copied: {len(files)}\n"
        f"- Source licence: {lic['type']}" + (f" ({lic['file']})" if lic["file"] else "") + "\n"
        "- Skipped: VCS/build/dependency folders, symlinks, secrets (.env*, *.pem, *.key, *.p12, *.pfx)\n"
    )
    if warnings:
        notes += "\n## Warnings\n\n" + "\n".join(f"- {w}" for w in warnings) + "\n"
    (dst / "CLONE_NOTES.md").write_text(notes, encoding="utf-8")
    return {
        "destination": str(dst),
        "files_copied": len(files),
        "total_kb": round(total / 1024, 1),
        "license": lic,
        "warnings": warnings,
    }


def clone_git_repo(url: str, destination: str) -> dict:
    """Shallow-clone a public https repository from an allowed host."""
    url = url.strip()
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname or u.hostname.lower() not in GIT_HOSTS or u.username or u.password:
        raise UltronError(f"Only plain https URLs on these hosts are allowed: {', '.join(sorted(GIT_HOSTS))}")
    if os.environ.get("ULTRON_ALLOW_NETWORK", "0").lower() not in {"1", "true", "yes", "on"}:
        raise UltronError("Outbound network is disabled in this sandbox. Set ULTRON_ALLOW_NETWORK=1 to allow git clone.")
    dst = _safe(destination)
    if dst.exists():
        raise UltronError(f"Destination already exists: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
        "clone", "--depth", "1", "--no-tags", "--", url, str(dst),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except FileNotFoundError:
        raise UltronError("git is not installed.")
    except subprocess.TimeoutExpired:
        shutil.rmtree(dst, ignore_errors=True)
        raise UltronError("git clone timed out.")
    if r.returncode != 0:
        shutil.rmtree(dst, ignore_errors=True)
        raise UltronError("git clone failed: " + r.stderr.strip()[-300:])
    lic = detect_license(dst)
    files, total = _collect(dst)
    return {
        "destination": str(dst),
        "files": len(files),
        "total_kb": round(total / 1024, 1),
        "license": lic,
        "warnings": _license_warnings(lic),
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def compare_projects(source: str, clone: str) -> dict:
    """Compare two directory trees by relative path and content hash."""
    a, b = _safe(source), _safe(clone)
    _require_dir(a)
    _require_dir(b)
    fa = {f.relative_to(a).as_posix(): f for f in _collect(a)[0]}
    fb = {f.relative_to(b).as_posix(): f for f in _collect(b)[0]}
    common = sorted(fa.keys() & fb.keys())
    modified = [p for p in common if _sha256(fa[p]) != _sha256(fb[p])]
    identical = len(common) - len(modified)
    return {
        "source_files": len(fa),
        "clone_files": len(fb),
        "identical": identical,
        "parity_percent": round(identical / len(fa) * 100, 1) if fa else None,
        "missing_in_clone": sorted(fa.keys() - fb.keys())[:LIST_CAP],
        "extra_in_clone": sorted(fb.keys() - fa.keys())[:LIST_CAP],
        "modified": modified[:LIST_CAP],
    }


# ------------------------------------------------------------ scaffolding ----

def _templates(name: str, desc: str) -> dict:
    lc = name.lower()
    readme = f"# {name}\n\n{desc}\n"
    gitignore = "__pycache__/\n.venv/\nnode_modules/\n.env\n"
    flask = {
        "app.py": (
            "from flask import Flask, jsonify\n\napp = Flask(__name__)\n\n\n"
            "@app.get(\"/\")\ndef index():\n"
            f"    return jsonify(status=\"ok\", app=\"{name}\")\n\n\n"
            "if __name__ == \"__main__\":\n    app.run(debug=True)\n"
        ),
        "requirements.txt": "Flask>=3.0\n",
        "README.md": readme + "\n## Run\n\n```\npip install -r requirements.txt\npython app.py\n```\n",
        ".gitignore": gitignore,
    }
    node = {
        "package.json": json.dumps(
            {"name": lc, "version": "1.0.0", "description": desc, "main": "server.js",
             "scripts": {"start": "node server.js"}, "dependencies": {"express": "^4.19.2"}},
            indent=2) + "\n",
        "server.js": (
            "const express = require(\"express\");\n\nconst app = express();\n"
            f"app.get(\"/\", (_req, res) => res.json({{ status: \"ok\", app: \"{name}\" }}));\n\n"
            "const port = process.env.PORT || 3000;\n"
            f"app.listen(port, () => console.log(\"{name} listening on \" + port));\n"
        ),
        "README.md": readme + "\n## Run\n\n```\nnpm install\nnpm start\n```\n",
        ".gitignore": gitignore,
    }
    static = {
        "index.html": (
            "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"utf-8\">\n"
            "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"  <title>{html.escape(name)}</title>\n  <link rel=\"stylesheet\" href=\"style.css\">\n</head>\n<body>\n"
            f"  <main>\n    <h1>{html.escape(name)}</h1>\n    <p>{html.escape(desc)}</p>\n  </main>\n"
            "  <script src=\"script.js\"></script>\n</body>\n</html>\n"
        ),
        "style.css": "body { font-family: system-ui, sans-serif; margin: 0; }\nmain { max-width: 48rem; margin: 3rem auto; padding: 0 1rem; }\n",
        "script.js": "document.addEventListener(\"DOMContentLoaded\", () => {\n  console.log(\"ready\");\n});\n",
        "README.md": readme + "\n## Run\n\nOpen `index.html` in a browser.\n",
    }
    return {"flask": flask, "node-express": node, "static": static}


def scaffold_project(name: str, stack: str, destination: str, description: str = "") -> dict:
    """Generate a minimal runnable project skeleton under destination/name."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
        raise UltronError("name must start with a letter and use only letters, digits, '-' or '_' (max 64).")
    description = (description.strip() or "Generated by Ultron.")[:300]
    tables = _templates(name, description)
    if stack not in tables:
        raise UltronError(f"stack must be one of: {', '.join(sorted(tables))}")
    root = _safe(destination) / name
    _safe(str(root))  # re-validate the final path
    if root.exists():
        raise UltronError(f"Already exists: {root}")
    for rel, content in tables[stack].items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return {"project": str(root), "stack": stack, "files": sorted(tables[stack])}
