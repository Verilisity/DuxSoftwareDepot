#!/usr/bin/env python3
"""Exercise what a DMS plugin update gets to show, offline.

Three things go wrong quietly here: which release tag belongs to which
plugin (a repository of twenty plugins tags by date, and a bare "v1.0.0"
there could be anyone's), which part of a changelog is the update being
installed, and whether an update in a shared repository touches this plugin
at all. The forge is stubbed — these are the parts that have to be right
before the network is worth asking.

Run from anywhere:  python3 scripts/test_plugin_notes.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import enrich
import plugin_notes as pn

failures = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FOUT ") + label)
    if not ok:
        print(f"         verwacht: {want!r}")
        print(f"         gekregen: {got!r}")
        failures.append(label)


print("Release-tags")
check("v1.2.0", pn.tag_plugin_version("v1.2.0", "x", "", False), "1.2.0")
check("zonder v", pn.tag_plugin_version("0.3.3", "x", "", False), "0.3.3")
check("release/1.0.4", pn.tag_plugin_version("release/1.0.4", "markets", "", False), "1.0.4")
check("dms/1.8.1-dms.4", pn.tag_plugin_version("dms/1.8.1-dms.4", "bitwarden", "", False), "1.8.1-dms.4")
check("gedeelde repo: tag met de pluginnaam",
      pn.tag_plugin_version("quickCapture-v5.6.0", "quickCapture", "quickCapture", True), "5.6.0")
check("gedeelde repo: tag van een andere plugin",
      pn.tag_plugin_version("timer-v5.6.0", "quickCapture", "quickCapture", True), "")
check("gedeelde repo: map-tag van een andere plugin",
      pn.tag_plugin_version("timer/5.6.0", "quickCapture", "quickCapture", True), "")
check("geen versie in de tag", pn.tag_plugin_version("latest", "x", "", False), "")

print("Bereik")
check("de nieuwe versie", pn.in_range("1.2.0", "1.1.0", "1.2.0"), True)
check("een overgeslagen versie", pn.in_range("1.1.5", "1.1.0", "1.2.0"), True)
check("de geïnstalleerde versie niet", pn.in_range("1.1.0", "1.1.0", "1.2.0"), False)
check("een nieuwere dan de update niet", pn.in_range("1.3.0", "1.1.0", "1.2.0"), False)
check("trailing zero", pn.in_range("1.2", "1.1.0", "1.2.0"), True)

print("Changelog")
CHANGELOG = """# Changelog

## [Unreleased]

- not out yet

## [1.2.0] - 2026-10-01

### Fixed

- the clock no longer drifts

## 1.1.5 — 2026-09-20

- smaller icon

## [1.1.0] - 2026-09-01

- first
"""
sections = pn.changelog_sections(CHANGELOG, "1.1.0", "1.2.0")
check("versies in bereik, nieuwste eerst", [v for v, _ in sections], ["1.2.0", "1.1.5"])
check("subkop hoort bij zijn versie", "### Fixed" in sections[0][1] and "drifts" in sections[0][1], True)
check("Unreleased lekt niet in een versie", any("not out yet" in b for _, b in sections), False)

# ── The forge, stubbed ──────────────────────────────────────────────────────
OLD, NEW = "aaaaaaa", "bbbbbbb"


def forge(releases=(), files=(), commits=(), path_commits=None, manifest='{"version": "1.2.0"}', raw=None):
    raw = dict(raw or {})

    def http_json(url, payload=None, timeout=15):
        if "/releases" in url:
            return list(releases)
        if "/compare/" in url:
            return {"html_url": "https://github.com/o/r/compare/x...y",
                    "files": [{"filename": f} for f in files],
                    "commits": [{"sha": s, "commit": {"message": m, "author": {"date": "2026-10-01T00:00:00Z"}}}
                                for s, m in commits]}
        if "/commits?" in url:
            return [{"sha": s, "commit": {"message": m}} for s, m in reversed(path_commits or [])]
        raise AssertionError("unexpected " + url)

    def raw_get(repo, ref, path):
        if path.endswith("plugin.json"):
            return manifest
        return raw.get(path)

    enrich._http_json = http_json
    pn._raw = raw_get


URL = f"https://github.com/o/r/compare/{OLD}...{NEW}"

print("Bronnen")
forge(releases=[{"tag_name": "v1.2.0", "body": "- new clock", "html_url": "rel"},
                {"tag_name": "v1.0.0", "body": "- old"}],
      raw={"CHANGELOG.md": CHANGELOG}, commits=[("c1", "Update clock")])
out = pn.plugin_notes(URL, "", "1.1.0", "clock")
check("een release wint", (out["kind"], [r["version"] for r in out["releases"]]), ("release", ["1.2.0"]))
check("nieuwe versie uit plugin.json", out["toVersion"], "1.2.0")

forge(raw={"CHANGELOG.md": CHANGELOG}, commits=[("c1", "Update clock")])
out = pn.plugin_notes(URL, "", "1.1.0", "clock")
check("zonder release: de changelog", (out["kind"], [r["version"] for r in out["releases"]]),
      ("changelog", ["1.2.0", "1.1.5"]))

forge(commits=[("c1", "Fix drift"), ("c2", "Merge pull request #3"), ("c3", "Bump to 1.2.0")])
out = pn.plugin_notes(URL, "", "1.1.0", "clock")
check("zonder beide: de commits, zonder merges", (out["kind"], out["commitCount"]), ("commits", 2))

forge(releases=[{"tag_name": "v1.2.0", "body": "- new clock"}], manifest='{"version": "1.1.0"}',
      commits=[("c1", "Fix typo")])
out = pn.plugin_notes(URL, "", "1.1.0", "clock")
check("zelfde versie: commits, niet de oude release", out["kind"], "commits")

print("Gedeelde repository")
forge(files=["timer/Timer.qml"], commits=[("c1", "timer: faster")])
out = pn.plugin_notes(URL, "quickCapture", "5.6.0", "quickCapture")
check("update raakt deze plugin niet", out["kind"], "untouched")

forge(files=["timer/Timer.qml", "quickCapture/Capture.qml"],
      commits=[("c1", "timer: faster"), ("c2", "quickCapture: fix crop")],
      path_commits=[("c0", "older, outside the range"), ("c2", "quickCapture: fix crop")],
      manifest='{"version": "5.6.0"}')
out = pn.plugin_notes(URL, "quickCapture", "5.6.0", "quickCapture")
check("alleen commits in de pluginmap en in het bereik",
      (out["kind"], out["commitCount"], out["releases"][0]["notesHtml"]),
      ("commits", 1, "<ul><li>quickCapture: fix crop</li></ul>"))

forge(releases=[{"tag_name": "v2026.10.04", "body": "- everything"}], files=["quickCapture/a.qml"],
      commits=[("c1", "quickCapture: x")], path_commits=[("c1", "quickCapture: x")],
      manifest='{"version": "5.7.0"}', raw={"CHANGELOG.md": "## 5.7.0\n- someone else's"})
out = pn.plugin_notes(URL, "quickCapture", "5.6.0", "quickCapture")
check("datum-tag en root-changelog van de repo tellen niet", out["kind"], "commits")

forge(releases=[{"tag_name": "v0.3.11", "body": "- sync resumes"}], files=["dms-plugin/a.qml"],
      manifest='{"version": "0.3.11"}')
out = pn.plugin_notes(URL, "dms-plugin", "0.3.10", "dankmailUnread")
check("plugin in een submap van zijn app: exacte kale tag telt", out["kind"], "release")

print("Invoer")
check("geen compare-link", pn.plugin_notes("https://github.com/o/r", "", "1.0", "x")["error"], "no compare link")

print()
if failures:
    print(f"{len(failures)} fout")
    sys.exit(1)
print("alles goed")
