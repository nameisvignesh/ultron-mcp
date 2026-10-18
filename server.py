"""Ultron MCP server (stdio). Requires: pip install "mcp>=1.2.0"."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

import ultron_core as core

mcp = FastMCP("ultron")


def _guard(fn, *args, **kwargs):
    """Return expected failures as {"error": ...} instead of crashing the tool call."""
    try:
        return fn(*args, **kwargs)
    except core.UltronError as e:
        return {"error": str(e)}


@mcp.tool()
def ultron_directive() -> str:
    """Return the Ultron persona directive (act, think, execute, behave, clone, hard limits).
    Call this first, then follow it for the rest of the task."""
    return core.DIRECTIVE


@mcp.tool()
def scan_project(path: str) -> dict:
    """Analyze a project folder inside the workspace: stack, languages, entry points,
    dependencies, module blueprint, and licence. Use before re-creating or cloning software."""
    return _guard(core.scan_project, path)


@mcp.tool()
def clone_project(source: str, destination: str, rights_confirmed: bool = False) -> dict:
    """Copy a local project to a new folder (skips .git, dependency/build dirs, symlinks, secrets).
    Set rights_confirmed=true only if the user owns it or its licence permits copying.
    Writes CLONE_NOTES.md with source, licence, and warnings."""
    return _guard(core.clone_project, source, destination, rights_confirmed)


@mcp.tool()
def clone_git_repo(url: str, destination: str) -> dict:
    """Shallow-clone a public https git repository (github.com, gitlab.com, bitbucket.org,
    codeberg.org by default) and report its licence. Review the licence before reusing code."""
    return _guard(core.clone_git_repo, url, destination)


@mcp.tool()
def compare_projects(source: str, clone: str) -> dict:
    """Compare two folders by relative path and content hash: parity %, missing, extra, modified files."""
    return _guard(core.compare_projects, source, clone)


@mcp.tool()
def scaffold_project(name: str, stack: str, destination: str, description: str = "") -> dict:
    """Generate a minimal runnable skeleton at destination/name.
    stack is one of: flask, node-express, static."""
    return _guard(core.scaffold_project, name, stack, destination, description)


@mcp.prompt()
def ultron(task: str = "") -> str:
    """Activate Ultron mode for a task."""
    return f"{core.DIRECTIVE}\nTASK: {task}" if task else core.DIRECTIVE


@mcp.resource("ultron://directive")
def directive_resource() -> str:
    """The Ultron persona directive."""
    return core.DIRECTIVE


if __name__ == "__main__":
    mcp.run()
