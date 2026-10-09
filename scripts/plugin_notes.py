#!/usr/bin/env python3
"""What changes when a DMS plugin updates.

The daemon decides a plugin has an update by comparing the commit it has
checked out with the head of the remote branch (`git ls-remote`, no fetch),
and hands over a compare link between the two. That is all the shell knows:
no version, no notes. So this asks the forge, the best answer winning:

  1. the GitHub release for the new version, plus any release skipped on
     the way — what the author chose to write for users;
  2. the section of the plugin's CHANGELOG.md for those versions;
  3. the commit subjects between the two, limited to the plugin's own
     directory when it lives in a repository with others.

A survey of the registry (370 plugins, October 2026) found matching releases
for 58 and a changelog file for 50; the commits are there for every one, so
every plugin update gets something. In a repository of twenty plugins the
daemon reports an update for all twenty whenever one of them changes, and
the commits are also how this tells an update that touches nothing of this
plugin apart from one that does.

Usage:  plugin_notes.py <diff-url> <path-in-repo> <installed-version> <plugin-id>
Prints one JSON object, the same shape enrich.py --gitnotes gives, plus
toVersion (the new plugin.json's version, "" when unknown):

  {"kind": "release"|"changelog"|"commits"|"untouched"|"", "toVersion",
   "url", "releases": [{version, date, notesHtml, newer}], "more",
   "commitCount", "repo", "error"}
"""
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import enrich  # noqa: E402

CACHE = os.path.join(enrich.CACHE_DIR, "plugin-notes-cache.json")
COMPARE_RE = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?/compare/([0-9a-f]{7,40})\.\.\.([0-9a-f]{7,40})/?$")
CHANGELOG_NAMES = ("CHANGELOG.md", "Changelog.md", "changelog.md", "CHANGES.md")
# "## [1.2.0] - 2026-10-01", "## 1.2.0 — 2026-10-01", "### v1.2.0", "# Version 1.2"
CHANGELOG_HEADING_RE = re.compile(r"^(#{1,4})\s*(?:version\s+)?\[?v?(\d+(?:\.\d+)+(?:[-+][\w.]+)?)\]?", re.I)


def _http_text(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "dankSoftwareDepot/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _raw(repo, ref, path):
    try:
        return _http_text(f"https://raw.githubusercontent.com/{repo}/{ref}/{path}")
    except Exception:
        return None


def tag_plugin_version(tag, plugin_id, path, shared):
    """The version a release tag names for this plugin, or "".

    Plugins tag "v1.2.0", "1.2.0", "release/1.2.0" or "dms/1.8.1-dms.4". In a
    repository shared with other plugins a bare version could be anyone's,
    so there a tag that names the plugin ("quickCapture-v1.2.0") is taken
    for any version, and a bare one only when it is exactly the version
    being installed — a repository of twenty plugins tagged by date never
    gets there, a plugin kept in a subdirectory of its app's repository
    does."""
    tag = (tag or "").strip()
    if shared:
        names = {n.lower() for n in (plugin_id, os.path.basename(path.strip("/"))) if n}
        m = re.match(r"^(.+?)[-_/@]v?(\d[\w.+-]*)$", tag)
        if m and m.group(1).lower() in names:
            return m.group(2)
        # "timer/5.6.0" is another plugin's, whatever its number
        m = re.match(r"^v?(\d[\w.+-]*)$", tag)
        return m.group(1) if m else ""
    m = re.match(r"^(?:[\w.-]+/)?v?(\d[\w.+-]*)$", tag)
    return m.group(1) if m else ""


def in_range(version, from_version, to_version):
    """version is the one being installed, or one skipped on the way to it."""
    if not version:
        return False
    if to_version and enrich.tags_match(version, to_version):
        return True
    if not from_version or not enrich.version_newer(version, from_version):
        return False
    return not to_version or not enrich.version_newer(version, to_version)


def from_releases(repo, plugin_id, path, shared, from_version, to_version):
    data = enrich._http_json(f"https://api.github.com/repos/{repo}/releases?per_page=30", timeout=20)
    if not isinstance(data, list):
        return None
    wanted = []
    for rel in data:
        if rel.get("draft"):
            continue
        version = tag_plugin_version(rel.get("tag_name"), plugin_id, path, shared)
        named = shared and version and not re.match(r"^v?\d", rel.get("tag_name") or "")
        if shared and not named and not enrich.tags_match(version, to_version):
            continue
        if in_range(version, from_version, to_version):
            wanted.append((version, rel))
    if not wanted:
        return None
    wanted.sort(key=lambda vr: enrich.version_key(vr[0]), reverse=True)
    releases, dropped = [], 0
    for version, rel in wanted[:enrich.MAX_RELEASES]:
        notes, more = enrich.markdown_notes_html(rel.get("body") or "")
        dropped += more
        releases.append({
            "version": version,
            "date": enrich.normalize_date(rel.get("published_at") or ""),
            "notesHtml": notes,
            "newer": True,
        })
    return {
        "kind": "release",
        "more": dropped,
        "url": wanted[0][1].get("html_url") or f"https://github.com/{repo}/releases",
        "releases": releases,
    }


def changelog_sections(text, from_version, to_version):
    """[(version, markdown body)] for the headings in range, newest first.

    A section runs to the next heading of the same or a higher level, so the
    "### Fixed" inside "## 1.2.0" stays part of it."""
    sections, current, level = [], None, 0
    for line in text.splitlines():
        m = CHANGELOG_HEADING_RE.match(line)
        if m and (current is None or len(m.group(1)) <= level):
            if current:
                sections.append(current)
            current, level = (m.group(2), []), len(m.group(1))
            continue
        heading = re.match(r"^(#{1,4})\s", line)
        if current and heading and len(heading.group(1)) <= level:
            sections.append(current)
            current = None  # "## Unreleased" and the like end a section
            continue
        if current:
            current[1].append(line)
    if current:
        sections.append(current)
    out = [(v, "\n".join(body)) for v, body in sections if in_range(v, from_version, to_version)]
    out.sort(key=lambda vb: enrich.version_key(vb[0]), reverse=True)
    return out


def from_changelog(repo, ref, path, shared, from_version, to_version):
    # The repository's own changelog speaks for a repository of one plugin;
    # beside nineteen others it is not this plugin's
    dirs = [path.strip("/")] if path.strip("/") else []
    if not shared:
        dirs.append("")
    for d in dirs:
        for name in CHANGELOG_NAMES:
            rel = (d + "/" if d else "") + name
            text = _raw(repo, ref, rel)
            if not text:
                continue
            sections = changelog_sections(text, from_version, to_version)
            if not sections:
                return None
            releases, dropped = [], 0
            for version, body in sections[:enrich.MAX_RELEASES]:
                notes, more = enrich.markdown_notes_html(body)
                dropped += more
                releases.append({"version": version, "date": 0, "notesHtml": notes, "newer": True})
            return {
                "kind": "changelog",
                "more": dropped,
                "url": f"https://github.com/{repo}/blob/{ref}/{rel}",
                "releases": releases,
            }
    return None


def touches(files, path):
    prefix = path.strip("/") + "/"
    return any((f.get("filename") or "").startswith(prefix) for f in files)


def from_commits(repo, old, new, path, shared, compare, to_version):
    import html as htmlmod
    url = compare.get("html_url") or f"https://github.com/{repo}/compare/{old}...{new}"
    commits = compare.get("commits") or []
    if shared and path.strip("/"):
        # The compare lists every commit in the repository; the commits
        # endpoint can filter by path but not by range. Together they give
        # this plugin's commits in this range.
        in_range_shas = {c.get("sha") for c in commits}
        listed = enrich._http_json(
            f"https://api.github.com/repos/{repo}/commits?sha={new}&path={path.strip('/')}&per_page=100",
            timeout=20)
        commits = [c for c in reversed(listed or []) if c.get("sha") in in_range_shas]
        url = f"https://github.com/{repo}/commits/{new}/{path.strip('/')}"
    subjects = []
    for commit in reversed(commits):
        subject = ((commit.get("commit") or {}).get("message") or "").split("\n")[0].strip()
        if subject and not subject.startswith("Merge "):
            subjects.append(subject)
    if not subjects:
        return None
    shown = subjects[:enrich.GITNOTES_MAX_COMMITS]
    author = ((commits[-1].get("commit") or {}).get("author") or {}) if commits else {}
    return {
        "kind": "commits",
        "commitCount": len(subjects),
        "more": max(0, len(subjects) - len(shown)),
        "url": url,
        "releases": [{
            "version": to_version or new[:7],
            "date": enrich.normalize_date(author.get("date", "")),
            "notesHtml": "<ul>" + "".join("<li>" + htmlmod.escape(s) + "</li>" for s in shown) + "</ul>",
            "newer": True,
        }],
    }


def plugin_notes(diff_url, path, from_version, plugin_id, shared=None):
    empty = {"kind": "", "toVersion": "", "url": "", "releases": [], "more": 0, "commitCount": 0}
    m = COMPARE_RE.match((diff_url or "").strip())
    if not m:
        return {**empty, "error": "no compare link"}
    repo, old, new = m.groups()
    path = (path or "").strip("/")
    # A plugin with a path lives in a directory of a repository it shares
    if shared is None:
        shared = bool(path)
    out = {**empty, "repo": repo}

    manifest = _raw(repo, new, (path + "/" if path else "") + "plugin.json")
    to_version = ""
    try:
        to_version = (json.loads(manifest).get("version") or "").strip() if manifest else ""
    except ValueError:
        pass
    out["toVersion"] = to_version

    compare = enrich._http_json(f"https://api.github.com/repos/{repo}/compare/{old}...{new}", timeout=20)
    files = compare.get("files")
    # Up to 300 files are listed; a list that long proves nothing either way
    if shared and path and isinstance(files, list) and len(files) < 300 and not touches(files, path):
        return {**out, "kind": "untouched", "url": compare.get("html_url") or ""}

    bumped = to_version and from_version and enrich.version_newer(to_version, from_version)
    for source in (
        (lambda: from_releases(repo, plugin_id, path, shared, from_version, to_version)) if bumped else None,
        (lambda: from_changelog(repo, new, path, shared, from_version, to_version)) if bumped else None,
        lambda: from_commits(repo, old, new, path, shared, compare, to_version),
    ):
        if source is None:
            continue
        try:
            found = source()
        except Exception:
            found = None
        if found:
            return {**out, **found}
    return {**out, "error": "nothing upstream"}


def main():
    if len(sys.argv) < 5:
        print(__doc__.split("Usage:")[1].split("\n")[0].strip(), file=sys.stderr)
        sys.exit(2)
    diff_url, path, from_version, plugin_id = sys.argv[1:5]
    key = "|".join((diff_url, path, from_version))
    cache = enrich._load_daily_cache(CACHE)
    if key in cache["data"]:
        json.dump(cache["data"][key], sys.stdout)
        return
    try:
        out = plugin_notes(diff_url, path, from_version, plugin_id)
    except Exception as exc:
        # Rate limit, no network: the popup shows the manifest regardless,
        # and an error is not worth remembering for a day
        json.dump({"kind": "", "toVersion": "", "url": "", "releases": [], "more": 0,
                   "commitCount": 0, "error": str(exc)}, sys.stdout)
        return
    if not cache["ts"]:
        cache["ts"] = time.time()
    cache["data"][key] = out
    try:
        os.makedirs(enrich.CACHE_DIR, exist_ok=True)
        with open(CACHE, "w") as f:
            json.dump(cache, f)
    except OSError:
        pass
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
