import json
import os
import py_compile
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ["ULTRON_WORKSPACE"] = _tmp

import ultron_core as c  # noqa: E402

W = Path(c.WORKSPACE)


def expect_error(fn, *a, **k):
    try:
        fn(*a, **k)
    except c.UltronError:
        return
    raise AssertionError(f"expected UltronError from {fn.__name__}{a}")


def make_sample():
    s = W / "sample"
    (s / "src").mkdir(parents=True)
    (s / "node_modules" / "x").mkdir(parents=True)
    (s / "app.py").write_text("print('hi')\n")
    (s / "src" / "util.py").write_text("x = 1\n")
    (s / "node_modules" / "x" / "i.js").write_text("//\n")
    (s / "requirements.txt").write_text("Flask>=3.0\n# c\nrequests==2.0\n")
    (s / "package.json").write_text(json.dumps({"dependencies": {"express": "1"}}))
    (s / ".env").write_text("SECRET=1\n")
    (s / ".env.example").write_text("SECRET=\n")
    (s / "key.pem").write_text("k")
    (s / "README.md").write_text("# Sample App\n")
    (s / "LICENSE").write_text("MIT License\n\nPermission is hereby granted, free of charge, to any person\n")
    return s


make_sample()


def test_scan():
    r = c.scan_project("sample")
    assert r["file_count"] == 7, r  # app.py util.py reqs pkg .env.example README LICENSE
    assert r["languages"]["Python"] == 2
    assert "Python" in r["stack"] and "Node.js" in r["stack"]
    assert r["entrypoints"] == ["app.py"]
    assert r["dependencies"]["python"] == ["Flask", "requests"]
    assert r["dependencies"]["node"] == ["express"]
    assert r["license"]["type"] == "MIT"
    assert r["readme_title"] == "Sample App"


def test_paths():
    expect_error(c.scan_project, "../../etc")
    expect_error(c.scan_project, "/etc")
    link = W / "escape"
    try:
        link.symlink_to(tempfile.gettempdir())
    except (OSError, NotImplementedError):
        return
    expect_error(c.scan_project, "escape")


def test_clone_and_compare():
    expect_error(c.clone_project, "sample", "clone1")  # no rights flag
    r = c.clone_project("sample", "clone1", rights_confirmed=True)
    assert r["files_copied"] == 7 and r["license"]["type"] == "MIT" and not r["warnings"]
    d = W / "clone1"
    assert not (d / ".env").exists() and not (d / "key.pem").exists()
    assert not (d / "node_modules").exists() and (d / ".env.example").exists()
    assert (d / "CLONE_NOTES.md").exists()
    expect_error(c.clone_project, "sample", "clone1", rights_confirmed=True)  # exists
    expect_error(c.clone_project, "sample", "sample/inner", rights_confirmed=True)
    cmp1 = c.compare_projects("sample", "clone1")
    assert cmp1["parity_percent"] == 100.0 and cmp1["extra_in_clone"] == ["CLONE_NOTES.md"]
    (d / "app.py").write_text("print('changed')\n")
    (d / "src" / "util.py").unlink()
    cmp2 = c.compare_projects("sample", "clone1")
    assert cmp2["modified"] == ["app.py"] and cmp2["missing_in_clone"] == ["src/util.py"]
    assert cmp2["parity_percent"] == round(5 / 7 * 100, 1)


def test_unlicensed_warning():
    u = W / "nolic"
    u.mkdir()
    (u / "a.py").write_text("1\n")
    r = c.clone_project("nolic", "nolic-copy", rights_confirmed=True)
    assert r["license"]["type"] == "none found" and r["warnings"]


def test_git_url_validation():
    for bad in ("http://github.com/a/b", "file:///etc", "https://evil.example/a/b",
                "https://user:pw@github.com/a/b", "git@github.com:a/b.git", "--upload-pack=x"):
        expect_error(c.clone_git_repo, bad, "g1")
    assert not (W / "g1").exists()
    # valid URL but sandbox has no network: must fail cleanly and leave nothing behind
    expect_error(c.clone_git_repo, "https://github.com/octocat/Hello-World", "g2")
    assert not (W / "g2").exists()


def test_scaffold():
    for stack in ("flask", "node-express", "static"):
        r = c.scaffold_project(f"My-App_{stack[:2]}", stack, "out", 'A "quoted" <desc>')
        root = Path(r["project"])
        assert all((root / f).is_file() for f in r["files"])
        if stack == "flask":
            py_compile.compile(str(root / "app.py"), doraise=True)
        if stack == "node-express":
            pkg = json.loads((root / "package.json").read_text())
            assert pkg["name"].islower() and pkg["description"] == 'A "quoted" <desc>'
        if stack == "static":
            assert "&lt;desc&gt;" in (root / "index.html").read_text()
    expect_error(c.scaffold_project, "bad name!", "flask", "out")
    expect_error(c.scaffold_project, "../x", "flask", "out")
    expect_error(c.scaffold_project, "ok", "django", "out")
    expect_error(c.scaffold_project, "My-App_fl", "flask", "out")  # exists


def test_directive():
    assert "ULTRON" in c.DIRECTIVE and "HARD LIMITS" in c.DIRECTIVE


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok ", name)
    print("ALL PASSED")
