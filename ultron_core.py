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

# ------------------------------------------------------- ULTRON modes ----
# Register these functions as MCP tools in server.py.


def deep_dive(topic: str, path: str = "") -> dict:
    """Build a detailed, implementation-oriented analysis brief for a topic."""
    topic = (topic or "").strip()
    if not topic:
        raise UltronError("Provide a topic for deep analysis.")
    result = {
        "mode": "ULTRON_DEEP=TRUE",
        "topic": topic[:500],
        "analysis_framework": [
            "Define the concept and the problem it solves.",
            "Explain the underlying mechanism step by step.",
            "Identify components, interfaces, state, and data flow.",
            "Compare viable designs and state trade-offs.",
            "Cover failure modes, security risks, performance, and scaling.",
            "Provide a concrete example and implementation plan.",
            "List assumptions and tests that would validate the design.",
        ],
        "response_instruction": (
            "Produce a detailed technical explanation using the framework above. "
            "Separate established facts from assumptions; include examples and edge cases."
        ),
    }
    if path.strip():
        result["project_context"] = scan_project(path)
    return result


_SECRET_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("AWS access key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("generic credential assignment", re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|passwd)\b\s*[:=]\s*['\"]?[^'\"\s]{8,}"
    )),
]


def secrets_audit(path: str = ".") -> dict:
    """Find likely hard-coded secrets; never return matched values or file contents."""
    root = _safe(path)
    _require_dir(root)
    findings = []
    scanned = 0
    for file_path in _iter_files(root):
        try:
            if file_path.stat().st_size > 2 * 1024 * 1024:
                continue
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeError):
            continue
        scanned += 1
        for line_no, line in enumerate(content.splitlines(), 1):
            for label, pattern in _SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append({
                        "file": file_path.relative_to(root).as_posix(),
                        "line": line_no,
                        "type": label,
                        "value": "[REDACTED]",
                    })
                    break
            if len(findings) >= LIST_CAP:
                break
        if len(findings) >= LIST_CAP:
            break
    return {
        "mode": "ULTRON_HUSH=true",
        "root": str(root),
        "files_scanned": scanned,
        "finding_count": len(findings),
        "findings": findings,
        "note": "Matches are heuristic. Values and source lines are deliberately not returned.",
    }


def architecture(path: str = ".", write: bool = False, overwrite: bool = False) -> dict:
    """Create a Mermaid architecture blueprint from the project structure."""
    root = _safe(path)
    _require_dir(root)
    scan = scan_project(str(root))
    modules = scan["blueprint"][:12]
    lines = ["flowchart TD", '    A["Application / Repository"]']
    for index, module in enumerate(modules, 1):
        safe_id = f"M{index}"
        label = module["module"].replace('"', "'")[:70]
        lines.append(f'    {safe_id}["{label}<br/>{module["files"]} files"]')
        lines.append(f"    A --> {safe_id}")
    if scan["entrypoints"]:
        lines.append('    E["Entry points"]')
        lines.append("    A --> E")
        for index, entry in enumerate(scan["entrypoints"][:8], 1):
            label = entry.replace('"', "'")[:70]
            lines.append(f'    E --> EP{index}["{label}"]')
    diagram = "\n".join(lines) + "\n"
    markdown = (
        "# Architecture Blueprint\n\n"
        f"- Root: `{root}`\n"
        f"- Files scanned: {scan['file_count']}\n"
        f"- Languages: {', '.join(scan['languages']) or 'Not detected'}\n"
        f"- Stack markers: {', '.join(scan['stack']) or 'Not detected'}\n\n"
        "```mermaid\n" + diagram + "```\n"
    )
    output_path = root / "ARCHITECTURE.md"
    written = False
    if write:
        if output_path.exists() and not overwrite:
            raise UltronError("ARCHITECTURE.md already exists. Set overwrite=true to replace it.")
        output_path.write_text(markdown, encoding="utf-8")
        written = True
    return {
        "mode": "ULTRON_ARC=true",
        "root": str(root),
        "diagram_format": "Mermaid",
        "diagram": diagram,
        "summary": {
            "file_count": scan["file_count"],
            "languages": scan["languages"],
            "stack": scan["stack"],
            "entrypoints": scan["entrypoints"][:20],
        },
        "written": written,
        "output": str(output_path) if written else None,
        "markdown": markdown[:12000],
    }


# Local-only browser IDE. Edits are held in the browser textarea and never saved.
_IDE_SERVERS = {}
_IDE_LOCK = __import__("threading").Lock()
_IDE_MAX_READ = 1024 * 1024


def ide(path: str = ".", port: int = 5000, stop: bool = False) -> dict:
    """Start/stop a local browser IDE. Edits remain in browser memory and are not saved."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse
    import threading

    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise UltronError("port must be an integer between 1024 and 65535.")
    root = _safe(path)
    _require_dir(root)
    key = (str(root), port)

    with _IDE_LOCK:
        existing = _IDE_SERVERS.get(key)
        if stop:
            if not existing:
                return {"stopped": False, "message": "No IDE server was registered for this workspace and port."}
            existing.shutdown()
            existing.server_close()
            _IDE_SERVERS.pop(key, None)
            return {"stopped": True, "port": port}
        if existing:
            return {"running": True, "url": f"http://127.0.0.1:{port}", "root": str(root),
                    "message": "IDE is already running."}

        page = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ULTRON IDE</title><style>
:root{color-scheme:dark;font:14px system-ui;background:#10131a;color:#e8edf5}*{box-sizing:border-box}
body{margin:0;height:100vh;display:flex;flex-direction:column}header{padding:12px 18px;background:#191f2b;border-bottom:1px solid #30394a;display:flex;gap:14px;align-items:center}
header strong{letter-spacing:2px;color:#8bd5ff}#status{color:#9aa9bd;font-size:12px}main{display:grid;grid-template-columns:260px 1fr;flex:1;min-height:0}
aside{overflow:auto;border-right:1px solid #30394a;padding:12px}button{background:#1b2330;border:1px solid #354156;color:#e8edf5;padding:7px 10px;border-radius:6px;cursor:pointer}
.file{display:block;width:100%;text-align:left;margin:4px 0;overflow-wrap:anywhere}.file:hover,button:hover{border-color:#8bd5ff}section{display:flex;flex-direction:column;min-width:0}
#filename{padding:10px 14px;background:#151b25;color:#8bd5ff}textarea{resize:none;flex:1;width:100%;border:0;outline:0;background:#0d1117;color:#dbe7f5;padding:18px;font:13px/1.6 Consolas,monospace;tab-size:4}
footer{padding:8px 14px;background:#191f2b;color:#9aa9bd;font-size:12px}@media(max-width:650px){main{grid-template-columns:145px 1fr}aside{padding:6px}.file{font-size:11px;padding:6px}}
</style></head><body><header><strong>ULTRON // IDE</strong><span id="status">LOCAL WORKSPACE</span><button id="reset">Reload file</button></header>
<main><aside id="files">Loading workspace…</aside><section><div id="filename">Select a file</div><textarea id="editor" spellcheck="false" aria-label="Session-only code editor" placeholder="Choose a file from the explorer…"></textarea>
<footer>Session-only editor · Changes are not written to disk · Code execution disabled</footer></section></main>
<script>let current='',original='';const $=id=>document.getElementById(id);
async function list(){const r=await fetch('/api/files');const d=await r.json();$('files').innerHTML='';if(!r.ok){$('files').textContent=d.error||'Unable to list files';return}
d.files.forEach(f=>{const b=document.createElement('button');b.className='file';b.textContent=f;b.onclick=()=>openFile(f);$('files').appendChild(b)})}
async function openFile(f){const r=await fetch('/api/read?path='+encodeURIComponent(f));const d=await r.json();if(!r.ok){$('status').textContent=d.error||'Read failed';return}
current=f;original=d.content;$('filename').textContent=f;$('editor').value=original;$('status').textContent='SESSION EDIT · NOT SAVED'}
$('reset').onclick=()=>{$('editor').value=original;$('status').textContent='Reverted to disk version'};list();</script></body></html>'''

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def _send(self, status, body, content_type="application/json; charset=utf-8"):
                payload = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    self._send(200, page, "text/html; charset=utf-8")
                    return
                if parsed.path == "/api/files":
                    files = []
                    for f in _iter_files(root):
                        try:
                            rel = f.relative_to(root).as_posix()
                            if f.stat().st_size <= _IDE_MAX_READ:
                                files.append(rel)
                        except OSError:
                            continue
                        if len(files) >= LIST_CAP:
                            break
                    self._send(200, json.dumps({"files": files}))
                    return
                if parsed.path == "/api/read":
                    rel = parse_qs(parsed.query).get("path", [""])[0]
                    candidate = (root / rel).resolve()
                    if candidate != root and root not in candidate.parents:
                        self._send(403, json.dumps({"error": "Path is outside the IDE workspace."}))
                        return
                    if not candidate.is_file() or candidate.is_symlink() or _skip_file(candidate.name):
                        self._send(404, json.dumps({"error": "File not found or not readable."}))
                        return
                    try:
                        if candidate.stat().st_size > _IDE_MAX_READ:
                            self._send(413, json.dumps({"error": "File exceeds 1 MB read limit."}))
                            return
                        content = candidate.read_text(encoding="utf-8")
                    except (OSError, UnicodeError):
                        self._send(415, json.dumps({"error": "File is not readable UTF-8 text."}))
                        return
                    self._send(200, json.dumps({"path": rel, "content": content}))
                    return
                self._send(404, json.dumps({"error": "Not found."}))

            def do_POST(self):
                self._send(405, json.dumps({"error": "Writes and code execution are disabled."}))

        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as exc:
            raise UltronError(f"Could not start IDE on 127.0.0.1:{port}: {exc}") from exc
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, name=f"ultron-ide-{port}", daemon=True)
        _IDE_SERVERS[key] = server
        thread.start()
    return {"running": True, "url": f"http://127.0.0.1:{port}", "host": "127.0.0.1", "root": str(root),
            "editable": True, "persistence": "session-only; no disk writes", "execution": "disabled"}


ULTRON_COMMANDS = {
    "ULTRON_DEEP=TRUE": "Call ultron_deep_analysis(topic, path?) for implementation-level concept exploration.",
    "ULTRON_HUSH=true": "Call ultron_hush(path?) to audit for likely secrets; returned values are always redacted.",
    "ULTRON_ARC=true": "Call ultron_architecture(path?, write?, overwrite?) for a Mermaid architecture blueprint.",
    "ULTRON_IDE=TRUE": "Call ultron_ide_start(path?, port=5000, stop?) to launch the local session-only browser IDE.",
}


def ultron_command_help() -> dict:
    """Describe supported ULTRON command triggers and their corresponding MCP tools."""
    return {"commands": ULTRON_COMMANDS, "default_workspace": str(WORKSPACE)}



def ultron_mode(
    command: str,
    topic: str = "",
    path: str = ".",
    port: int = 5000,
    write: bool = False,
    overwrite: bool = False,
    stop: bool = False,
) -> dict:
    """Dispatch one of the four explicit ULTRON command strings to its feature."""
    normalized = (command or "").strip().upper()
    if normalized == "ULTRON_DEEP=TRUE":
        return deep_dive(topic, path if path and path != "." else "")
    if normalized == "ULTRON_HUSH=TRUE":
        return secrets_audit(path)
    if normalized == "ULTRON_ARC=TRUE":
        return architecture(path, write=write, overwrite=overwrite)
    if normalized == "ULTRON_IDE=TRUE":
        return ide(path, port=port, stop=stop)
    raise UltronError(
        "Unknown ULTRON command. Use ULTRON_DEEP=TRUE, ULTRON_HUSH=true, "
        "ULTRON_ARC=true, or ULTRON_IDE=TRUE."
    )
