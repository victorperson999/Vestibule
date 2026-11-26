"""Static site checks (M5 step 2, plan docs/plans/M5/M5-step2-site.md §4).

Pure Python stdlib + pytest only (zero PyYAML or Node dependency, zero skips):
verifies site structure, offline asset hygiene, zero dangerous DOM sinks, data
parity with config.py / README.md / docs/REFERENCE.md / SECURITY.md, case-sensitive
GitHub source links, V7a Hero wording, Pages workflow supply-chain hygiene, and
PyPI sdist/wheel exclusion of site/.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import tomllib
import zipfile
from html.parser import HTMLParser
from pathlib import Path

from vestibule.config import Limits

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
INDEX_HTML = SITE / "index.html"
STYLE_CSS = SITE / "style.css"
APP_JS = SITE / "app.js"
DEMO_GIF = SITE / "assets" / "demo.gif"
LOGO_SVG = SITE / "assets" / "logo.svg"
FAVICON_SVG = SITE / "favicon.svg"
NOJEKYLL = SITE / ".nojekyll"
PAGES_YML = ROOT / ".github" / "workflows" / "pages.yml"
GITATTRIBUTES = ROOT / ".gitattributes"

_REQUIRED_FILES = (INDEX_HTML, STYLE_CSS, APP_JS, DEMO_GIF, LOGO_SVG, FAVICON_SVG, NOJEKYLL, PAGES_YML)
_SUBRESOURCE_ATTRS = {
    "script": ("src",),
    "link": ("href",),
    "img": ("src",),
    "source": ("src", "srcset"),
    "iframe": ("src",),
}
_GITHUB_BLOB_RE = re.compile(
    r"https://github\.com/victorperson999/Vestibule/blob/main/([^\s\"'<>#?]+)",
    re.I,
)
_SPEEDUP_RE = re.compile(r"\b[0-9]+(?:\.[0-9]+)?\s*[x×]\b", re.I)
_SHA_PIN_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}(?:\s+#.*)?$")
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img",
    "input", "link", "meta", "param", "source", "track", "wbr",
})


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exists_exactly(base: Path, rel: str) -> bool:
    """Case-sensitive existence check under base (even on Windows/macOS)."""
    parts = Path(os.path.normpath(rel)).parts
    if not parts or ".." in parts or Path(rel).is_absolute():
        return False
    here = base
    for part in parts:
        try:
            if part not in os.listdir(here):
                return False
        except OSError:
            return False
        here = here / part
    return True


class _SiteHTMLAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inline_scripts: list[tuple[dict[str, str | None], str]] = []
        self.external_scripts: list[dict[str, str | None]] = []
        self.style_tags = 0
        self.event_attrs: list[tuple[str, str]] = []
        self.style_attrs: list[str] = []
        self.external_subresources: list[tuple[str, str, str]] = []
        self.local_refs: list[tuple[str, str, str]] = []
        self.blob_links: list[str] = []
        self.roles: list[tuple[str, dict[str, str | None]]] = []
        self.hero_text_parts: list[str] = []
        self.benchmarks_text_parts: list[str] = []
        self.all_text_parts: list[str] = []
        self._in_script: dict[str, str | None] | None = None
        self._script_buf: list[str] = []
        self._hero_depth = 0
        self._bench_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        if tag == "style":
            self.style_tags += 1
        for k, v in attrs:
            kl = k.lower()
            if kl.startswith("on"):
                self.event_attrs.append((tag, k))
            if kl == "style":
                self.style_attrs.append(tag)
            if v and kl in ("href", "src"):
                self.blob_links.extend(_GITHUB_BLOB_RE.findall(v))
        for attr_name in _SUBRESOURCE_ATTRS.get(tag, ()):
            val = (attr_map.get(attr_name) or "").strip()
            if not val:
                continue
            low = val.lower()
            if low.startswith(("http://", "https://", "//")):
                self.external_subresources.append((tag, attr_name, val))
            elif not low.startswith(("#", "data:", "mailto:")):
                self.local_refs.append((tag, attr_name, val.split("#", 1)[0].split("?", 1)[0]))
        if tag == "a":
            href = (attr_map.get("href") or "").strip()
            low = href.lower()
            if href and not low.startswith(("http://", "https://", "//", "#", "mailto:")):
                self.local_refs.append((tag, "href", href.split("#", 1)[0].split("?", 1)[0]))
        if tag == "script":
            if "src" in attr_map and attr_map["src"]:
                self.external_scripts.append(attr_map)
            else:
                self._in_script = attr_map
                self._script_buf = []
        if "role" in attr_map:
            self.roles.append((tag, attr_map))
        elem_id = attr_map.get("id") or ""
        if tag not in _VOID_TAGS:
            if self._hero_depth > 0 or elem_id == "hero":
                self._hero_depth += 1
            if self._bench_depth > 0 or elem_id == "benchmarks":
                self._bench_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_script is not None:
            self.inline_scripts.append((self._in_script, "".join(self._script_buf)))
            self._in_script = None
            self._script_buf = []
        if self._hero_depth > 0:
            self._hero_depth -= 1
        if self._bench_depth > 0:
            self._bench_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_script is not None:
            self._script_buf.append(data)
            return
        self.all_text_parts.append(data)
        if self._hero_depth > 0:
            self.hero_text_parts.append(data)
        if self._bench_depth > 0:
            self.benchmarks_text_parts.append(data)


def _audit_html() -> _SiteHTMLAudit:
    parser = _SiteHTMLAudit()
    parser.feed(INDEX_HTML.read_text(encoding="utf-8"))
    parser.close()
    return parser


def _vestibule_data() -> dict:
    audit = _audit_html()
    assert len(audit.inline_scripts) == 1
    return json.loads(audit.inline_scripts[0][1])


def test_required_site_files_exist_with_lf_line_endings():
    missing = [str(p.relative_to(ROOT)) for p in _REQUIRED_FILES if not p.is_file()]
    assert missing == [], f"missing site files: {missing}"
    assert NOJEKYLL.stat().st_size == 0
    for text_file in (INDEX_HTML, STYLE_CSS, APP_JS, LOGO_SVG, FAVICON_SVG, PAGES_YML):
        raw = text_file.read_bytes()
        assert b"\r\n" not in raw, f"CRLF line endings in {text_file.relative_to(ROOT)}"


def test_gitattributes_pins_site_lf_and_gif_binary():
    lines = {line.strip() for line in GITATTRIBUTES.read_text(encoding="utf-8").splitlines()}
    for rule in (
        "site/*.html text eol=lf",
        "site/*.css text eol=lf",
        "site/*.js text eol=lf",
        "site/assets/*.gif -text",
        ".github/workflows/pages.yml text eol=lf",
    ):
        assert rule in lines, f"missing .gitattributes rule: {rule}"


def test_site_demo_gif_byte_matches_latest_published_gif():
    test_docs = _load_module("test_docs_ref", ROOT / "tests" / "test_docs.py")
    latest_rel, _sha = test_docs._PUBLISHED_GIFS[-1]
    assert DEMO_GIF.read_bytes() == (ROOT / latest_rel).read_bytes()


def test_html_structure_zero_external_subresources_and_inline_hygiene():
    audit = _audit_html()
    assert audit.style_tags == 0, "inline <style> tags are forbidden"
    assert audit.event_attrs == [], f"inline event handlers forbidden: {audit.event_attrs}"
    assert audit.style_attrs == [], f"inline style attributes forbidden: {audit.style_attrs}"
    assert audit.external_subresources == [], (
        f"external subresources forbidden: {audit.external_subresources}"
    )

    assert len(audit.inline_scripts) == 1, "expected exactly one inline <script> data block"
    attrs, body = audit.inline_scripts[0]
    assert attrs.get("id") == "vestibule-data"
    assert attrs.get("type") == "application/json"
    assert "</script" not in body.lower()
    assert "<!--" not in body
    json.loads(body)

    # Every relative local reference resolves case-sensitively inside site/
    assert audit.local_refs, "expected local references (style.css, app.js, assets/demo.gif)"
    missing_local = [
        (tag, attr, ref)
        for tag, attr, ref in audit.local_refs
        if not _exists_exactly(SITE, ref)
    ]
    assert missing_local == [], f"broken local site refs: {missing_local}"

    # Verify CSS and JS have no external URLs or @import
    css = STYLE_CSS.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    for label, content in (("site/style.css", css), ("site/app.js", js)):
        assert not re.search(r"url\(\s*['\"]?(?:https?:|//)", content, re.I), (
            f"external url() in {label}"
        )
        assert not re.search(r"@import\b", content, re.I), f"@import forbidden in {label}"

    # WAI-ARIA tablist walkthrough structure
    tablists = [a for _, a in audit.roles if a.get("role") == "tablist"]
    tabs = [a for _, a in audit.roles if a.get("role") == "tab"]
    panels = [a for _, a in audit.roles if a.get("role") == "tabpanel"]
    assert len(tablists) == 1
    assert len(tabs) == 4
    assert len(panels) == 4
    panel_ids = {a.get("id") for a in panels}
    assert {a.get("aria-controls") for a in tabs} == panel_ids


def test_app_js_has_no_dangerous_dom_sinks_or_fetch():
    js = APP_JS.read_text(encoding="utf-8")
    forbidden = (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "fetch(",
        ".style.",
        'setAttribute("style"',
        "setAttribute('style'",
    )
    hits = [token for token in forbidden if token in js]
    assert hits == [], f"forbidden JS sinks in site/app.js: {hits}"


def test_vestibule_data_matches_config_limits_defaults():
    data = _vestibule_data()
    defaults = data["defaults"]
    limits = Limits()
    assert defaults["mem_mb"] == limits.mem_mb
    assert defaults["cpu_pct"] == limits.cpu_pct
    assert defaults["default_timeout_s"] == limits.default_timeout_s
    assert defaults["max_timeout_s"] == limits.max_timeout_s
    assert defaults["workspace_dir"] == limits.workspace_dir


def test_readme_commands_and_caveats_parity():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    html_raw = INDEX_HTML.read_text(encoding="utf-8")
    data = _vestibule_data()
    data_str = json.dumps(data)
    combined = html_raw + "\n" + data_str

    win_uv = 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
    assert win_uv in readme
    assert data["platforms"]["windows"]["uv_cmd"] == win_uv

    # Client config paths from README.md must match #vestibule-data verbatim
    cursor_path = "~/.cursor/mcp.json"
    win_desktop_path = r"%APPDATA%\Claude\claude_desktop_config.json"
    mac_desktop_path = "~/Library/Application Support/Claude/claude_desktop_config.json"
    for cfg_path in (cursor_path, win_desktop_path, mac_desktop_path):
        assert cfg_path in readme, f"README.md missing expected config path: {cfg_path}"
    assert data["clients"]["cursor"]["config_path"] == cursor_path
    assert data["platforms"]["windows"]["claude_desktop_path"] == win_desktop_path
    assert data["platforms"]["macos"]["claude_desktop_path"] == mac_desktop_path

    for required in (
        "open a new terminal",
        "Ubuntu 23.10+ (host or VM, not WSL2)",
        "log out/in",
        "Docker snap",
        "no-new-privileges",
        "root-equivalent",
        "uv tool dir --bin",
        "/usr/local/bin:/usr/bin:/bin",
        "tested on Windows",
        "cmd /c --%",
        "/plugin",
        "vestibule-mcp audit",
        "vestibule-mcp ui",
    ):
        assert required in combined, f"missing expected caveat/command text: {required!r}"

    # macOS explicitly marked untested
    mac = data["platforms"]["macos"]
    assert "untested" in mac["note"].lower()
    assert "untested" in mac["claude_desktop_status"].lower()

    # Linux, WSL2, Ubuntu Claude Desktop honest note (no fabricated Linux path)
    for plat_key in ("linux", "wsl2", "ubuntu"):
        plat = data["platforms"][plat_key]
        assert "claude_desktop_path" not in plat
        assert "does not ship an official Linux Claude Desktop build" in plat["claude_desktop_note"]
    assert "Windows" in data["platforms"]["wsl2"]["claude_desktop_note"]


def test_benchmarks_exact_numbers_verbatim_caveat_and_no_speedup_claims():
    ref_lines = (ROOT / "docs" / "REFERENCE.md").read_text(encoding="utf-8").splitlines()
    caveat_line = next(line for line in ref_lines if "The container path here goes through" in line)
    want_caveat = caveat_line.split(" So these are this host's numbers, not a speed-up factor.")[0]
    want_caveat = (want_caveat + " So these are this host's numbers, not a speed-up factor.").replace(
        "`", ""
    )

    audit = _audit_html()
    bench_text = " ".join("".join(audit.benchmarks_text_parts).split())
    for num in ("118", "141", "895", "1002"):
        assert num in bench_text, f"missing measured benchmark figure {num}"
    assert want_caveat in bench_text, "verbatim REFERENCE.md caveat missing from #benchmarks"
    assert not _SPEEDUP_RE.search(bench_text), (
        f"speedup-ratio wording forbidden in #benchmarks: {_SPEEDUP_RE.findall(bench_text)}"
    )
    assert "faster than docker" not in bench_text.lower()


def test_github_blob_links_resolve_case_sensitively_in_repo():
    audit = _audit_html()
    data_str = json.dumps(_vestibule_data())
    links = set(audit.blob_links) | set(_GITHUB_BLOB_RE.findall(data_str))
    assert "src/vestibule/backends/container.py" in links
    assert "SECURITY.md" in links
    assert "scripts/bench_coldstart.py" in links
    missing = sorted(rel for rel in links if not _exists_exactly(ROOT, rel))
    assert missing == [], f"broken GitHub blob/main links: {missing}"


def test_hero_v7a_boundary_without_early_development_banner():
    audit = _audit_html()
    assert audit._hero_depth == 0, f"#hero depth leaked: {audit._hero_depth}"
    assert audit._bench_depth == 0, f"#benchmarks depth leaked: {audit._bench_depth}"
    hero_text = " ".join("".join(audit.hero_text_parts).split())
    assert "Not a VM" in hero_text
    assert "Honest Threat Model" not in hero_text
    assert "early development" not in hero_text.lower()


def test_site_is_excluded_from_sdist_and_wheel(tmp_path):
    with open(ROOT / "pyproject.toml", "rb") as f:
        project = tomllib.load(f)
    sdist_inc = project["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    wheel_pkgs = project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert all("site" not in entry for entry in sdist_inc)
    assert all("site" not in entry for entry in wheel_pkgs)

    release_smoke = _load_module("release_smoke_ref", ROOT / "scripts" / "release_smoke.py")
    good_sdist = [
        "PKG-INFO",
        "LICENSE",
        "README.md",
        "PYPI.md",
        "pyproject.toml",
        "src/vestibule/__init__.py",
    ]
    assert release_smoke.sdist_problems([*good_sdist, "site/index.html"]) == [
        "not allowed in the sdist: site/index.html"
    ]

    whl_path = tmp_path / "test.whl"
    with zipfile.ZipFile(whl_path, "w") as zf:
        for req in release_smoke._WHEEL_REQUIRED:
            zf.writestr(req, b"ok\n")
        zf.writestr("vestibule_mcp-0.1.2.dist-info/METADATA", b"Name: vestibule-mcp\n")
        zf.writestr("site/index.html", b"<!doctype html>\n")
    with zipfile.ZipFile(whl_path) as zf:
        assert "not allowed in the wheel: site/index.html" in release_smoke.wheel_problems(zf)


def test_pages_workflow_supply_chain_hygiene():
    raw = PAGES_YML.read_bytes()
    assert b"\r\n" not in raw, "pages.yml must use LF"
    text = raw.decode("utf-8")
    assert "\t" not in text, "pages.yml must not contain tabs"

    lines = text.splitlines()
    assert any(line.strip() == "workflow_dispatch:" for line in lines)
    # Post-release S3-D3: push trigger must be strictly scoped to main and site/**
    assert re.search(
        r"^  push:\n    branches:\n      - main\n    paths:\n      - \"site/\*\*\"\s*$",
        text,
        re.M,
    ), "pages.yml push trigger must be strictly scoped to branches: [main] and paths: ['site/**']"

    # Top-level contents: read; two-job build -> deploy layout (avoids deploy-pages#451 race)
    assert re.search(r"^permissions:\n  contents: read\b", text, re.M)
    assert re.search(r"^  build:\n(?:    [^\n]+\n)*?    permissions:\n      contents: read\b", text, re.M)
    assert re.search(r"^  deploy:\n    needs: build\b", text, re.M)
    assert re.search(
        r"^  deploy:\n(?:    [^\n]+\n)*?    permissions:\n(?:      [^\n]+\n)*?      pages: write\b",
        text,
        re.M,
    )
    assert re.search(
        r"^  deploy:\n(?:    [^\n]+\n)*?    permissions:\n(?:      [^\n]+\n)*?      id-token: write\b",
        text,
        re.M,
    )
    assert len(re.findall(r"^\s+timeout-minutes:\s*[1-9][0-9]*\b", text, re.M)) == 2
    assert "persist-credentials: false" in text
    assert re.search(r"^\s+path:\s*site\s*$", text, re.M)

    uses_values = [
        m.group(1).strip()
        for line in lines
        if (m := re.match(r"^\s*(?:-\s*)?uses:\s*(.+)$", line))
    ]
    assert len(uses_values) == 4, f"expected 4 pinned actions, found {uses_values}"
    unpinned = [u for u in uses_values if not _SHA_PIN_RE.match(u)]
    assert unpinned == [], f"unpinned action(s) in pages.yml: {unpinned}"
