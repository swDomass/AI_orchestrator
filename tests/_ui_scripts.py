"""Helper for tests that run the dashboard page's JavaScript in node.

The page used to be one Python string (``dashboard._HTML_PAGE``); it now lives
in ``dashboard_ui/``. ``page_script()`` rebuilds the former single script text
from ``index.html`` so the probes stay unchanged: it appends the page's own
scripts in the order index.html loads them.
"""

import json
import shutil
import subprocess
from html.parser import HTMLParser

import dashboard

_VENDOR = (
    "/static/vendor/chart.js-4.6.1/chart.umd.min.js",
    "/static/vendor/chartjs-plugin-zoom-1.2.1/chartjs-plugin-zoom.min.js",
)


class _ScriptTags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[tuple[str | None, bool]] = []  # (src, in head)
        self._in_head = False
        self._open: int | None = None
        self.inline_content = ""

    def handle_starttag(self, tag, attrs):
        if tag == "head":
            self._in_head = True
        elif tag == "script":
            self.scripts.append((dict(attrs).get("src"), self._in_head))
            self._open = len(self.scripts) - 1

    def handle_endtag(self, tag):
        if tag == "head":
            self._in_head = False
        elif tag == "script":
            self._open = None

    def handle_data(self, data):
        if self._open is not None:
            self.inline_content += data


def page_script() -> str:
    """Own scripts of index.html concatenated in load order (vendor excluded)."""
    parser = _ScriptTags()
    parser.feed((dashboard._UI_DIR / "index.html").read_text(encoding="utf-8"))
    assert parser.inline_content.strip() == "", "inline <script> content"
    assert parser.scripts and all(src for src, _ in parser.scripts), "<script> without src"
    head = [src for src, in_head in parser.scripts if in_head]
    own = [src for src, in_head in parser.scripts if not in_head]
    assert head == list(_VENDOR), head
    assert own, "no own scripts"
    parts = []
    for src in own:
        assert src in dashboard._STATIC_FILES, src
        rel, _ = dashboard._STATIC_FILES[src]
        parts.append((dashboard._UI_DIR / rel).read_text(encoding="utf-8"))
    return "\n".join(parts)


class _Marked(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.buttons: list[dict[str, str]] = []
        self.boot_hidden: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if "data-action" in a:
            self.buttons.append(a)
        if "boot-hidden" in a.get("class", "").split():
            self.boot_hidden.append(a.get("id", ""))


def _marked() -> _Marked:
    parser = _Marked()
    parser.feed((dashboard._UI_DIR / "index.html").read_text(encoding="utf-8"))
    return parser


def index_buttons() -> list[dict[str, str]]:
    """Attributes (id, class, data-*) of every element in index.html carrying data-action."""
    return _marked().buttons


def boot_hidden_ids() -> list[str]:
    """ids of the elements index.html ships with the .boot-hidden class."""
    return _marked().boot_hidden


NODE = shutil.which("node")

# Minimal browser surface: enough for the page scripts to load and render.
NODE_STUB = r"""
const _els = {};
const _docListeners = {};
function _el(id) {
  if (!_els[id]) {
    _els[id] = {
      id, innerHTML: '', textContent: '', style: {}, dataset: {}, onclick: null,
      hidden: false, value: '',
      classList: { toggle() {}, add() {}, remove() {} },
      getContext() { return { canvasId: id }; },
      appendChild(c) { (this.children = this.children || []).push(c); },
      addEventListener() {},
      querySelectorAll() { return []; },
      scrollIntoView(o) { (globalThis._scrolls = globalThis._scrolls || []).push({ id, o }); },
      focus(o) { (globalThis._focused = globalThis._focused || []).push({ id, o }); document.activeElement = this; },
    };
  }
  return _els[id];
}
globalThis.document = {
  getElementById: _el,
  querySelectorAll() { return []; },
  querySelector() { return null; },
  addEventListener(type, fn) { (_docListeners[type] = _docListeners[type] || []).push(fn); },
  createElement() { return _el('__new' + Object.keys(_els).length); },
};
globalThis.window = globalThis;
globalThis.location = { hash: '', port: '8211', reload() { globalThis._reloaded = (globalThis._reloaded || 0) + 1; } };
const _charts = [];
globalThis.Chart = function (ctx, cfg) {
  this.canvasId = ctx.canvasId; this.data = cfg.data; this.options = cfg.options;
  _charts.push(this);
};
Chart.prototype.update = function () {};
Chart.prototype.resetZoom = function () {};
Chart.register = function () {};
globalThis.fetch = () => new Promise(() => {});  // never resolves; we call update() ourselves
globalThis.setInterval = () => 0;
globalThis.setTimeout = () => 0;
globalThis.clearTimeout = () => {};
"""


def run_node(tmp_path, source: str, name: str = "page_test.js") -> dict:
    """Write ``source`` to tmp_path, run it in node, return the last stdout line parsed as JSON."""
    js = tmp_path / name
    js.write_text(source, encoding="utf-8")
    assert NODE is not None
    proc = subprocess.run([NODE, str(js)], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "node printed nothing (a promise never settled?): " + proc.stderr[-500:]
    result: dict = json.loads(proc.stdout.strip().splitlines()[-1])
    return result
