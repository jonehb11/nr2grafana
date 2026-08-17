"""Interactive wizard for nr2grafana.

Launched by running `nr2grafana` / `n2g` / `g2n` with no arguments in a
terminal. Arrow-key menus (with a numbered fallback for terminals that
don't support raw mode), guided flows for the whole migration:

  fetch from New Relic -> convert -> validate -> live-check -> import.

Stdlib only. Secrets (API keys, Grafana tokens) are never persisted;
non-secret answers (dirs, URLs, region) are remembered in
~/.config/nr2grafana/state.json between runs.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional

from .config import DEFAULT_CONFIG
from .grafana.client import GrafanaClient, GrafanaError
from .livecheck import check_files
from .nerdgraph import NerdGraphClient, NerdGraphError

# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------

_ANSI = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None \
    and os.environ.get("TERM", "") != "dumb"


def _c(code: str, text: str) -> str:
    return "\033[%sm%s\033[0m" % (code, text) if _ANSI else text


def bold(t: str) -> str:
    return _c("1", t)


def dim(t: str) -> str:
    return _c("2", t)


def green(t: str) -> str:
    return _c("32", t)


def yellow(t: str) -> str:
    return _c("33", t)


def red(t: str) -> str:
    return _c("31", t)


def cyan(t: str) -> str:
    return _c("36", t)


def header(t: str) -> None:
    print()
    print(bold(cyan("── %s " % t)) + cyan("─" * max(0, 56 - len(t))))


_DEBUG_LOG = os.environ.get("N2G_DEBUG", "")


def _dbg(msg: str) -> None:
    if _DEBUG_LOG:
        import time
        with open(_DEBUG_LOG, "a") as f:
            f.write("%.3f %s\n" % (time.time(), msg))


def menu(title: str, options: List[str], default: int = 0) -> int:
    """Arrow-key menu; falls back to a numbered prompt. Returns the index."""
    print()
    print(bold(title))
    _dbg("menu: %r" % title[:40])
    if _ANSI and sys.stdin.isatty():
        try:
            idx = _arrow_menu(options, default)
            _dbg("menu returned %d" % idx)
            return idx
        except KeyboardInterrupt:
            raise
        except Exception as e:
            _dbg("arrow menu failed: %r" % e)
    return _numbered_menu(options, default)


def _arrow_menu(options: List[str], default: int) -> int:
    """Arrow-key selection. Raw mode is held for the WHOLE menu (entered
    before the first draw): entering raw per keypress leaves a window where
    a fast keystroke lands in cooked mode and gets stranded in the
    terminal's canonical line buffer. Raw mode disables output processing,
    so all drawing uses explicit \\r\\n line endings."""
    import select as _select
    import termios
    import tty
    fd = sys.stdin.fileno()
    idx = default
    n = len(options)

    def draw(first: bool) -> None:
        out = []
        if not first:
            out.append("\033[%dA" % n)  # cursor up n lines
        for i, opt in enumerate(options):
            marker = green("❯ ") if i == idx else "  "
            line = bold(opt) if i == idx else dim(opt)
            out.append("\r\033[2K%s%s\r\n" % (marker, line))
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def read_key() -> str:
        ch = os.read(fd, 1).decode("utf-8", "ignore")
        if ch == "\x1b":  # possible escape sequence (arrow keys)
            r, _, _ = _select.select([fd], [], [], 0.05)
            if r:
                ch += os.read(fd, 2).decode("utf-8", "ignore")
        return ch

    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        draw(True)
        while True:
            key = read_key()
            _dbg("key %r" % key)
            if key in ("\x1b[A", "k"):
                idx = (idx - 1) % n
            elif key in ("\x1b[B", "j"):
                idx = (idx + 1) % n
            elif key in ("\r", "\n"):
                return idx
            elif key in ("\x03", "q", "\x1b\x1b"):  # Ctrl-C / q
                raise KeyboardInterrupt
            elif key.isdigit() and 1 <= int(key) <= n:
                return int(key) - 1
            draw(False)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _numbered_menu(options: List[str], default: int) -> int:
    for i, opt in enumerate(options, 1):
        mark = "*" if i - 1 == default else " "
        print(" %s %d) %s" % (mark, i, opt))
    while True:
        raw = input("Choose [%d]: " % (default + 1)).strip()
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        print(red("enter a number between 1 and %d" % len(options)))


def prompt(text: str, default: str = "",
           validator: Optional[Callable[[str], str]] = None) -> str:
    """Prompt with a default. validator returns an error string or ''."""
    while True:
        suffix = dim(" [%s]" % default) if default else ""
        raw = input("%s%s: " % (text, suffix)).strip()
        value = raw or default
        if validator:
            err = validator(value)
            if err:
                print(red(err))
                continue
        return value


def prompt_secret(text: str, env_var: str = "") -> str:
    if env_var and os.environ.get(env_var):
        print("%s: %s" % (text, dim("(using $%s)" % env_var)))
        return os.environ[env_var]
    return getpass.getpass(text + ": ")


def confirm(text: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    raw = input("%s %s: " % (text, dim(suffix))).strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def _require_nonempty(v: str) -> str:
    return "" if v else "a value is required"


# ---------------------------------------------------------------------------
# Persistent (non-secret) state
# ---------------------------------------------------------------------------

_STATE_DIR = os.path.join(os.path.expanduser("~"), ".config", "nr2grafana")
_STATE_PATH = os.path.join(_STATE_DIR, "state.json")


def load_state() -> Dict[str, Any]:
    try:
        with open(_STATE_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: Dict[str, Any]) -> None:
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        with open(_STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except OSError:
        pass  # remembering answers is best-effort


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------

class Wizard:
    def __init__(self) -> None:
        self.state = load_state()

    def remember(self, key: str, value: Any) -> None:
        self.state[key] = value
        save_state(self.state)

    def recall(self, key: str, default: str = "") -> str:
        return str(self.state.get(key) or default)

    # -- flows --------------------------------------------------------------

    def run(self) -> int:
        print()
        print(bold(cyan("  nr2grafana")) + dim("  —  New Relic → Grafana "
                                               "dashboard migrator"))
        print(dim("  arrow keys / j k to move · Enter to select · "
                  "q or Ctrl-C to quit"))
        try:
            while True:
                choice = menu("What do you want to do?", [
                    "🚀  Full migration (fetch → convert → review)",
                    "📥  Fetch dashboards from New Relic",
                    "🔄  Convert New Relic JSON → Grafana JSON",
                    "✅  Validate converted dashboards",
                    "🔎  Live-check queries against Mimir/Loki",
                    "📤  Import dashboards into Grafana",
                    "⚙️   Choose / create a mapping config",
                    "👋  Quit",
                ])
                if choice == 0:
                    if self.flow_fetch() == 0:
                        if self.flow_convert() == 0:
                            self.offer_next_steps()
                elif choice == 1:
                    self.flow_fetch()
                elif choice == 2:
                    if self.flow_convert() == 0:
                        self.offer_next_steps()
                elif choice == 3:
                    self.flow_validate()
                elif choice == 4:
                    self.flow_livecheck()
                elif choice == 5:
                    self.flow_import()
                elif choice == 6:
                    self.flow_config()
                else:
                    print(dim("bye!"))
                    return 0
        except (KeyboardInterrupt, EOFError):
            print("\n" + dim("aborted."))
            return 130

    # -- fetch --------------------------------------------------------------

    def flow_fetch(self) -> int:
        header("Fetch dashboards from New Relic")
        api_key = prompt_secret("New Relic USER API key (NRAK-...)",
                                env_var="NEW_RELIC_API_KEY")
        if not api_key:
            print(red("an API key is required"))
            return 1
        region = ["US", "EU"][menu("New Relic region?", ["US", "EU"],
                                   default=0 if self.recall("region", "US")
                                   == "US" else 1)]
        self.remember("region", region)
        out = prompt("Save exported dashboards to",
                     self.recall("fetch_out", "./newrelic-dashboards"))
        self.remember("fetch_out", out)

        client = NerdGraphClient(api_key, region=region)
        print(dim("listing dashboards..."))
        try:
            entities = client.list_dashboards()
        except NerdGraphError as e:
            print(red("error: %s" % e))
            return 1
        print(green("found %d dashboards" % len(entities)))
        if not entities:
            return 1

        guids: List[str] = []
        scope = menu("Which dashboards?", [
            "All of them (%d)" % len(entities),
            "Filter by name substring",
        ])
        if scope == 1:
            while True:
                needle = prompt("Name contains", validator=_require_nonempty)
                hits = [e for e in entities
                        if needle.lower() in (e.get("name") or "").lower()]
                print("matched %d dashboard(s):" % len(hits))
                for e in hits[:20]:
                    print("   " + dim("- ") + (e.get("name") or "?"))
                if len(hits) > 20:
                    print(dim("   ... and %d more" % (len(hits) - 20)))
                if hits and confirm("Fetch these?"):
                    guids = [e["guid"] for e in hits]
                    break
                if not confirm("Try another filter?", default=True):
                    return 1

        from .cli import cmd_fetch
        args = argparse.Namespace(api_key=api_key, region=region, out=out,
                                  guid=guids)
        rc = cmd_fetch(args)
        if rc == 0:
            print(green("✓ fetch complete → %s" % out))
        return rc

    # -- convert ------------------------------------------------------------

    def flow_convert(self) -> int:
        header("Convert New Relic JSON → Grafana JSON")
        src = prompt("New Relic dashboard JSON file or directory",
                     self.recall("fetch_out", "./newrelic-dashboards"),
                     validator=lambda v: "" if os.path.exists(v)
                     else "no such file or directory")
        out = prompt("Write Grafana dashboards to",
                     self.recall("convert_out", "./grafana-dashboards"))
        self.remember("convert_out", out)

        config = self.pick_config()
        strategy = ["rows", "split"][menu(
            "Multi-page NR dashboards become...", [
                "One dashboard with collapsible rows per page (recommended)",
                "One Grafana dashboard per page (linked by a dropdown)",
            ])]
        passthrough = confirm(
            "Keep untranslatable widgets live via the New Relic Grafana "
            "datasource plugin? (needs the plugin installed)", default=False)

        from .cli import cmd_convert
        args = argparse.Namespace(inputs=[src], out=out, config=config,
                                  report="", page_strategy=strategy,
                                  passthrough=passthrough)
        rc = cmd_convert(args)
        self.summarize_report(os.path.join(out, "migration-report.json"))
        return rc

    def summarize_report(self, report_path: str) -> None:
        try:
            with open(report_path) as f:
                report = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        counts: Dict[str, int] = {}
        review: List[Dict[str, Any]] = []
        for rep in report.get("reports", []):
            for w in rep.get("widgets", []):
                counts[w["confidence"]] = counts.get(w["confidence"], 0) + 1
                if w["confidence"] in ("needs-review", "untranslatable"):
                    review.append(w)
        header("Conversion summary")
        for level, color in (("exact", green), ("approximate", cyan),
                             ("needs-review", yellow),
                             ("untranslatable", red)):
            if counts.get(level):
                print("  %s %d widget(s)" % (color("●"), counts[level])
                      + dim("  " + level))
        failed = report.get("failed_inputs") or []
        if failed:
            print(red("  ✗ %d input file(s) failed to convert" % len(failed)))
        if review:
            print()
            print(bold("Widgets to review first:"))
            for w in review[:8]:
                print("  %s %s / %s %s" % (
                    yellow("▸"), w.get("page", "?"),
                    w.get("widget", "?"),
                    dim("(" + w["confidence"] + ")")))
            if len(review) > 8:
                print(dim("  ... and %d more — see migration-report.json"
                          % (len(review) - 8)))

    def offer_next_steps(self) -> None:
        while True:
            nxt = menu("Next step?", [
                "Validate the converted dashboards",
                "Live-check queries against Mimir/Loki",
                "Import into Grafana",
                "Back to main menu",
            ], default=3)
            if nxt == 0:
                self.flow_validate()
            elif nxt == 1:
                self.flow_livecheck()
            elif nxt == 2:
                self.flow_import()
            else:
                return

    # -- validate -----------------------------------------------------------

    def flow_validate(self) -> int:
        header("Validate Grafana dashboard JSON")
        src = prompt("Dashboards file or directory",
                     self.recall("convert_out", "./grafana-dashboards"),
                     validator=lambda v: "" if os.path.exists(v)
                     else "no such file or directory")
        from .cli import cmd_validate
        rc = cmd_validate(argparse.Namespace(inputs=[src]))
        print(green("✓ all valid") if rc == 0 else
              red("✗ problems found (see above)"))
        return rc

    # -- livecheck ----------------------------------------------------------

    def flow_livecheck(self) -> int:
        header("Live-check queries against your stack")
        print(dim("Every PromQL/LogQL query is submitted to the real "
                  "engines; parse/label errors fail, empty results pass."))
        src = prompt("Dashboards file or directory",
                     self.recall("convert_out", "./grafana-dashboards"),
                     validator=lambda v: "" if os.path.exists(v)
                     else "no such file or directory")
        prom = prompt("Prometheus/Mimir query URL",
                      self.recall("prom_url", "http://localhost:9090"))
        loki = prompt("Loki URL",
                      self.recall("loki_url", "http://localhost:3100"))
        self.remember("prom_url", prom)
        self.remember("loki_url", loki)
        print()
        passed, failed, skipped, failures = check_files(
            [src], prom, loki, log=lambda m: None)
        for f in failures[:10]:
            print(red("  FAIL ") + f["target"])
            print(dim("       " + f["error"][:120]))
        print()
        print("%s passed, %s failed, %d skipped (tempo/passthrough)"
              % (green(str(passed)),
                 red(str(failed)) if failed else "0", skipped))
        return 1 if failed else 0

    # -- import -------------------------------------------------------------

    def flow_import(self) -> int:
        header("Import dashboards into Grafana")
        src = prompt("Dashboards file or directory",
                     self.recall("convert_out", "./grafana-dashboards"),
                     validator=lambda v: "" if os.path.exists(v)
                     else "no such file or directory")
        url = prompt("Grafana URL",
                     self.recall("grafana_url", "http://localhost:3000"))
        self.remember("grafana_url", url)
        auth = menu("Authentication?", [
            "Service account token (recommended)",
            "Username + password",
        ])
        if auth == 0:
            token = prompt_secret("Grafana token", env_var="GRAFANA_TOKEN")
            client = GrafanaClient(url, token=token)
        else:
            user = prompt("Username", "admin")
            pw = prompt_secret("Password")
            client = GrafanaClient(url, basic=(user, pw))
        try:
            info = client.health()
            print(green("✓ connected — Grafana %s"
                        % info.get("version", "?")))
        except GrafanaError as e:
            print(red("cannot connect: %s" % e))
            return 1

        folder_title = prompt("Folder to import into (blank = General)",
                              self.recall("folder", ""))
        self.remember("folder", folder_title)
        folder_uid = ""
        if folder_title:
            try:
                folder_uid = client.find_or_create_folder(folder_title)
            except GrafanaError as e:
                print(red("folder error: %s" % e))
                return 1
        overwrite = confirm(
            "Overwrite existing dashboards? (No = collisions fail loudly "
            "instead of replacing — safer for first imports)", default=False)

        from .livecheck import collect_files
        files = collect_files([src])
        if not files:
            print(red("no dashboard JSON files found"))
            return 1
        ok = 0
        problems: List[str] = []
        for path in files:
            with open(path) as f:
                dash = json.load(f)
            name = dash.get("title") or os.path.basename(path)
            try:
                res = client.import_dashboard(dash, folder_uid=folder_uid,
                                              overwrite=overwrite)
                ok += 1
                print("  %s %s %s" % (green("✓"), name,
                                      dim(res.get("url", ""))))
            except GrafanaError as e:
                msg = str(e)
                hint = ""
                if "name-exists" in msg or "version-mismatch" in msg:
                    hint = " (already exists — re-run with overwrite to " \
                           "replace)"
                problems.append(name)
                print("  %s %s%s" % (red("✗"), name, dim(hint)))
        print()
        print("%s imported, %s failed"
              % (green(str(ok)),
                 red(str(len(problems))) if problems else "0"))
        return 1 if problems else 0

    # -- config -------------------------------------------------------------

    def pick_config(self) -> str:
        found = self._known_configs()
        options = [os.path.relpath(p) for p in found]
        options.append("Built-in defaults (generic OTel → LGTM stack)")
        options.append("Enter a path manually")
        last = self.recall("config", "")
        default = 0
        if last in [os.path.relpath(p) for p in found]:
            default = [os.path.relpath(p) for p in found].index(last)
        elif not found:
            default = 0
        idx = menu("Mapping config? (labels/metrics/datasources for your "
                   "stack)", options, default=default)
        if idx == len(options) - 1:
            path = prompt("Config path", validator=lambda v: ""
                          if os.path.exists(v) else "no such file")
        elif idx == len(options) - 2:
            path = ""
        else:
            path = options[idx]
        self.remember("config", path)
        return path

    def _known_configs(self) -> List[str]:
        out: List[str] = []
        seen = set()
        for base in (".", os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))):
            cdir = os.path.join(base, "config")
            if not os.path.isdir(cdir):
                continue
            for name in sorted(os.listdir(cdir)):
                if name.startswith("mappings") and name.endswith(".json"):
                    p = os.path.join(cdir, name)
                    key = os.path.abspath(p)
                    if key not in seen:
                        seen.add(key)
                        out.append(p)
        return out

    def flow_config(self) -> int:
        header("Mapping configs")
        print(dim("A mapping config adapts conversion to your stack: label "
                  "names, metric names/types, Loki stream labels, "
                  "span-metrics flavor, datasource uids."))
        found = self._known_configs()
        options = ["Create a new config (guided stack questionnaire)"]
        options += ["Use %s" % os.path.relpath(p) for p in found]
        idx = menu("Configs", options)
        if idx == 0:
            return self._config_questionnaire()
        self.remember("config", os.path.relpath(found[idx - 1]))
        print(green("✓ selected %s" % self.recall("config")))
        return 0

    def _config_questionnaire(self) -> int:
        """Interview the user about their stack and write a mapping config.

        Covers everything deployment-specific, so no site details need to
        live in the repo. The curl commands shown discover each answer
        empirically from a workstation with access to the endpoints
        (port-forward or ingress — this tool never runs in-cluster).
        """
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))

        header("Datasources")
        if confirm("Pin concrete Grafana datasource uids? (No keeps "
                   "portable datasource-picker variables)", default=False):
            for fam, label in (("prometheus", "Mimir/Prometheus"),
                               ("loki", "Loki"), ("tempo", "Tempo")):
                cfg["datasources"][fam]["uid"] = prompt(
                    "%s datasource uid" % label,
                    cfg["datasources"][fam]["uid"])

        header("HTTP server metrics (for FROM Transaction widgets)")
        print(dim("Discover yours: curl <mimir>/api/v1/label/__name__/"
                  "values | grep http_server"))
        h = menu("Which HTTP server duration metric does your stack emit?", [
            "http_server_request_duration_seconds (current OTel semconv)",
            "http_server_duration_milliseconds (legacy semconv)",
            "Something else (custom name)",
            "Not sure yet (keep default, review later)",
        ])
        if h == 1:
            cfg["http_metrics_flavor"] = "legacy"
        elif h == 2:
            cfg["http_metrics"]["duration_histogram"] = prompt(
                "Histogram base name (without _bucket)",
                validator=_require_nonempty)
            cfg["http_metrics"]["unit"] = ["s", "ms"][menu(
                "Its unit?", ["seconds", "milliseconds"])]

        header("Span metrics (for FROM Span aggregations)")
        print(dim("Discover yours: curl <mimir>/api/v1/label/__name__/"
                  "values | grep -E 'spanmetrics|span_metrics'"))
        sm = menu("Which span-metrics generation exists in your Mimir?", [
            "traces_span_metrics_* in ms (OTel collector spanmetrics "
            "connector default)",
            "traces_span_metrics_duration_seconds (connector, seconds)",
            "traces_spanmetrics_latency (Tempo metrics-generator)",
            "Custom names",
            "Not sure yet (keep default, review later)",
        ])
        if sm == 1:
            cfg["spanmetrics_flavor"] = "otel-seconds"
        elif sm == 2:
            cfg["spanmetrics_flavor"] = "tempo"
            cfg["span_metrics"]["unit"] = ["ms", "s"][menu(
                "Latency histogram unit? (check your metrics-generator "
                "config)", ["milliseconds", "seconds"])]
            cfg["span_service_label"] = prompt(
                "Service identity label on span metrics", "service")
        elif sm == 3:
            cfg["span_metrics"]["duration_histogram"] = prompt(
                "Duration histogram base name", validator=_require_nonempty)
            cfg["span_metrics"]["calls_total"] = prompt(
                "Calls counter name", validator=_require_nonempty)
            cfg["span_metrics"]["unit"] = ["ms", "s"][menu(
                "Duration unit?", ["milliseconds", "seconds"])]
            cfg["span_service_label"] = prompt(
                "Service identity label on span metrics", "service_name")

        header("Custom metrics (FROM Metric widgets)")
        cfg["metric_total_suffix"] = menu(
            "Does your pipeline append the _total suffix to counters?", [
                "Yes — Prometheus exporter conventions (my.count -> "
                "my_count_total)",
                "No — prometheusremotewrite defaults (my.count -> my_count)",
            ]) == 0

        header("Loki")
        print(dim("Discover yours: curl <loki>/loki/api/v1/labels"))
        raw = prompt("Stream (index) labels, comma-separated",
                     ", ".join(cfg["loki_stream_labels"]))
        cfg["loki_stream_labels"] = [x.strip() for x in raw.split(",")
                                     if x.strip()]
        cfg["loki_parser"] = ["json", "logfmt", ""][menu(
            "Log body format?", ["JSON", "logfmt",
                                 "plain text (no parser stage)"])]
        raw = prompt("Structured-metadata labels (queryable without a "
                     "parser), comma-separated",
                     ", ".join(cfg["loki_metadata_labels"]))
        cfg["loki_metadata_labels"] = [x.strip() for x in raw.split(",")
                                       if x.strip()]

        path = prompt("Save config as", "config/mappings.mine.json")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        print(green("✓ wrote %s" % path))
        print(dim("  Attribute→label renames live in its label_map and "
                  "custom metric names in metric_map — extend them there "
                  "as real dashboards surface your naming."))
        self.remember("config", path)
        return 0


def run_wizard() -> int:
    if not sys.stdin.isatty():
        print("error: interactive mode needs a terminal "
              "(use subcommands: nr2grafana --help)", file=sys.stderr)
        return 2
    return Wizard().run()
