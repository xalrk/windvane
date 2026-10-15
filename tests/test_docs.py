"""The docs stay in step with the code: every config key and command documented, every relative link resolving, and
the CI workflow running the same test command windvane runs on itself."""
import contextlib
import io
import os
import re
import unittest
from pathlib import Path

from helpers import REPO

from windvane import cli, heavy
from windvane.config import DEFAULTS, STATE

DOCS = REPO / "docs"
PAGES = [REPO / "README.md", REPO / "CONTRIBUTING.md", REPO / "CHANGELOG.md", *sorted(DOCS.glob("*.md"))]
WORKFLOW = REPO / ".github/workflows/test.yml"


def prose(text):
    """Markdown without its fenced code blocks, where `[x](y)` is an example, not a link."""
    return re.sub(r"^ *(```|~~~).*?^ *\1[^\n]*$", "", text, flags=re.M | re.S)


def sections(text):
    """{heading: body} for the `## ` sections of a page."""
    return {m.group(1).strip(): m.group(2) for m in re.finditer(r"^## ([^\n]+)\n(.*?)(?=^## |\Z)", text, re.M | re.S)}


def slug(heading):
    """The anchor GitHub gives a heading."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def help_text(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.suppress(SystemExit):
        cli.main([*argv, "--help"])
    return out.getvalue()


def commands():
    """The subcommands `windvane --help` lists."""
    m = re.search(r"\{([a-z,-]+)\}", help_text())
    return m.group(1).split(",")


def yaml_subset(text):
    """Parse the block-style YAML a workflow uses (mappings, `- ` lists, `[a, b]` flow lists, quoted scalars), and fail
    loudly on anything else: the standard library has no YAML parser, and CI must not be the first to read the file."""
    rows = []
    for n, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[:len(raw) - len(raw.lstrip())]:
            raise ValueError(f"line {n}: a tab in the indentation")
        line = re.sub(r"(^|\s)#.*$", "", raw).rstrip()
        if line.strip():
            rows.append((len(line) - len(line.lstrip()), line.strip(), n))

    def scalar(s):
        if s.startswith("[") and s.endswith("]"):
            return [scalar(x.strip()) for x in s[1:-1].split(",") if x.strip()]
        if len(s) > 1 and s[0] == s[-1] and s[0] in "'\"":
            return s[1:-1]
        if s[:1] in "[{&*!|>'\"" or ": " in s:
            raise ValueError(f"unsupported YAML scalar: {s!r}")
        return {"true": True, "false": False, "null": None}.get(s, int(s) if s.isdigit() else s)

    def block(i, indent):
        """Parse the rows at `indent` from row i; returns (value, next row)."""
        if rows[i][1].startswith("- "):
            out = []
            while i < len(rows) and rows[i][0] == indent and rows[i][1].startswith("- "):
                rest = rows[i][1][2:]
                if re.match(r"[\w.-]+:( |$)", rest):  # a mapping item: re-read it as a row two columns in
                    rows[i] = (indent + 2, rest, rows[i][2])
                    item, i = block(i, indent + 2)
                else:
                    item, i = scalar(rest), i + 1
                out.append(item)
            return out, i
        out = {}
        while i < len(rows) and rows[i][0] == indent:
            m = re.fullmatch(r"([\w.-]+):(?: +(.*))?", rows[i][1])
            if not m:
                raise ValueError(f"line {rows[i][2]}: not `key: value`: {rows[i][1]!r}")
            key, val = m.group(1), m.group(2)
            if key in out:
                raise ValueError(f"line {rows[i][2]}: {key} given twice")
            i += 1
            if val is not None:
                out[key] = scalar(val)
            elif i < len(rows) and rows[i][0] > indent:
                out[key], i = block(i, rows[i][0])
            elif i < len(rows) and rows[i][0] == indent and rows[i][1].startswith("- "):
                out[key], i = block(i, indent)
            else:
                out[key] = None
        if i < len(rows) and rows[i][0] > indent:
            raise ValueError(f"line {rows[i][2]}: unexpected indentation")
        return out, i

    value, end = block(0, rows[0][0])
    if end != len(rows):
        raise ValueError(f"line {rows[end][2]}: unexpected dedent")
    return value


class ConfigReference(unittest.TestCase):
    longMessage = False  # the message names what's missing; the whole page would bury it

    def test_every_config_key_is_documented_in_its_section(self):
        ref = sections((DOCS / "configuration.md").read_text())
        for key, val in DEFAULTS.items():
            if isinstance(val, dict):
                body = ref.get(f"[{key}]", "")
                self.assertTrue(body, f"configuration.md has no `## [{key}]` section")
                for sub in val:
                    self.assertIn(f"| `{sub}` |", body, f"[{key}] {sub} isn't in configuration.md's table")
            else:
                self.assertIn(f"| `{key}` |", ref["Top level"], f"{key} isn't in configuration.md's table")

    def test_machine_settings_are_documented(self):
        body = sections((DOCS / "configuration.md").read_text())["machine.toml"]
        for key in [*heavy.DEFAULTS, "projects_dir"]:
            self.assertIn(f"| `{key}` |", body)
        state = sections((DOCS / "configuration.md").read_text())["[state]"]
        for key in STATE:
            self.assertIn(f"| `{key}` |", state, f"[state] {key} isn't in configuration.md's table")


class CommandReference(unittest.TestCase):
    longMessage = False  # the message names what's missing; the whole page would bury it

    def test_every_command_and_option_is_in_the_command_reference(self):
        ref = (DOCS / "commands.md").read_text()
        cmds = commands()
        self.assertGreater(len(cmds), 20)  # the help parsed
        for cmd in cmds:
            self.assertRegex(ref, rf"`windvane {cmd}\b", f"windvane {cmd} isn't in docs/commands.md")
            for opt in set(re.findall(r"(?<![\w-])--[a-z][a-z-]+", help_text(cmd))) - {"--help"}:
                self.assertIn(opt, ref, f"windvane {cmd} {opt} isn't in docs/commands.md")

    def test_phone_actions_are_documented(self):
        actions = re.search(r"\{([a-z,]+)\}", help_text("phone")).group(1).split(",")
        ref = (DOCS / "commands.md").read_text() + (DOCS / "phone.md").read_text()
        for action in actions:
            self.assertRegex(ref, rf"\b{action}\b")


class Links(unittest.TestCase):
    longMessage = False  # the message names what's missing; the whole page would bury it

    def test_every_relative_link_and_anchor_resolves(self):
        checked = 0
        for page in PAGES:
            for target in re.findall(r"\]\(([^)\s]+)\)", re.sub(r"`[^`\n]*`", "", prose(page.read_text()))):
                if re.match(r"[a-z]+:", target):
                    continue
                path, _, anchor = target.partition("#")
                f = (page.parent / path).resolve() if path else page
                where = f"{page.relative_to(REPO)}: {target}"
                self.assertTrue(f.exists(), f"{where}: no such file")
                if anchor:
                    heads = re.findall(r"^#+ (.+)$", prose(f.read_text()), re.M)
                    self.assertIn(anchor, [slug(h) for h in heads], f"{where}: no such heading")
                checked += 1
        self.assertGreater(checked, 30)

    def test_the_link_check_catches_a_broken_link(self):
        self.assertEqual(slug("[autopilot]"), "autopilot")
        self.assertEqual(slug("The critic and calibration"), "the-critic-and-calibration")
        self.assertEqual(re.findall(r"\]\(([^)\s]+)\)", prose("[a](x.md)\n```\n[b](y.md)\n```\n")), ["x.md"])

    def test_docs_hold_no_machine_paths(self):
        for page in PAGES:
            self.assertNotRegex(page.read_text(), r"/home/\w|/mnt/\w|/Users/\w", page.name)


class Workflow(unittest.TestCase):
    def setUp(self):
        self.wf = yaml_subset(WORKFLOW.read_text())

    def test_runs_by_hand_on_the_oldest_and_newest_python(self):
        """Hosted minutes are limited: the local check runs every Python, the workflow only on request."""
        job = self.wf["jobs"]["test"]
        self.assertEqual([s["run"] for s in job["steps"] if "run" in s], ["python3 -m unittest discover -s tests -q"])
        self.assertEqual(self.wf["on"], {"workflow_dispatch": None})
        script = REPO / "tools/check_pythons.sh"
        self.assertTrue(os.access(script, os.X_OK), "the local check is an executable script")
        local = re.search(r'^VERSIONS="([^"]*)"', script.read_text(), re.M).group(1).split()
        self.assertEqual(job["strategy"]["matrix"]["python"], [local[0], local[-1]], "the oldest and newest")
        hosted = {*job["strategy"]["matrix"]["python"], *(i["python"] for i in job["strategy"]["matrix"]["include"])}
        self.assertLessEqual(hosted, set(local), "the local check runs every Python the workflow names")

    def test_the_subset_parser_agrees_with_pyyaml(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML isn't installed")
        full = yaml.safe_load(WORKFLOW.read_text())
        full["on"] = full.pop(True)  # YAML 1.1 reads the bare key `on` as true
        self.assertEqual(self.wf, full)

    def test_the_subset_parser_rejects_broken_yaml(self):
        for bad in ("a: 1\n  b: 2\n", "a:\n\t- x\n", "a: 1\na: 2\n", "a: {x: 1}\n", "a\n"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                yaml_subset(bad)


if __name__ == "__main__":
    unittest.main()
