#!/usr/bin/env python3
"""Checks that what `apt-get upgrade` keeps back is held, with the reason.

The daemon offers everything `apt list --upgradable` lists and then runs
`apt-get upgrade`, which installs no new packages and removes none. What it
keeps back used to come out of a run as "not updated — try again". These
cases build a small local repository with one package per reason, install
the old versions, and check pkg_backend.holds() against both the expected
reasons and what apt-get itself says it keeps back.

It installs packages and writes to /etc/apt, so it only runs where it is
told it may: as root on a Debian-family system with DSD_APT_SANDBOX=1 —
the debian:13 container in CI, or one of your own. Anywhere else it skips.

    podman run --rm -e DSD_APT_SANDBOX=1 -v "$PWD/scripts:/dsd:ro,Z" \\
        docker.io/library/debian:13 sh -c \\
        'apt-get update -qq && apt-get install -y -qq python3-apt apt-utils \\
         && python3 /dsd/test_apt_kept_back.py'
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if os.environ.get("DSD_APT_SANDBOX") != "1" or os.geteuid() != 0 \
        or not shutil.which("dpkg-deb") or not shutil.which("apt-ftparchive"):
    print("overgeslagen: alleen als root in een wegwerp-Debian met DSD_APT_SANDBOX=1")
    sys.exit(0)

import pkg_backend  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(("ok   " if ok else "FOUT ") + label + ("" if ok else f": {got!r} != {want!r}"))
    if not ok:
        failures.append(label)


def run(*argv, **kw):
    res = subprocess.run(argv, capture_output=True, text=True, **kw)
    if res.returncode != 0:
        sys.exit(f"{' '.join(argv)} failed ({res.returncode}):\n{res.stderr}")
    return res.stdout


work = tempfile.mkdtemp()
os.chmod(work, 0o755)  # apt fetches file: sources as the _apt user
old_dir = os.path.join(work, "old")
repo = os.path.join(work, "repo")
os.makedirs(old_dir)
os.makedirs(repo)


def deb(name, version, dest, extra=""):
    root = os.path.join(work, "build", f"{name}_{version}")
    os.makedirs(os.path.join(root, "DEBIAN"))
    with open(os.path.join(root, "DEBIAN", "control"), "w") as f:
        f.write(f"Package: {name}\nVersion: {version}\nArchitecture: all\n"
                f"Maintainer: t <t@t>\nDescription: test {name}\n{extra}")
    run("dpkg-deb", "-b", root, os.path.join(dest, f"{name}_{version}_all.deb"))


# Installed at 1.0; the repository offers the newer version
deb("qs", "1.0", old_dir)
deb("qs", "1.1", repo)
deb("shellpkg", "1.0", old_dir, "Depends: qs (= 1.0)\n")      # pins qs
deb("foo", "1.0", old_dir)
deb("foo", "2.0", repo, "Depends: newdep\n")                  # needs a new package
deb("newdep", "1.0", repo)
deb("victim", "1.0", old_dir)
deb("brk", "1.0", old_dir)
deb("brk", "2.0", repo, "Breaks: victim (<< 2)\n")            # breaks what is installed
deb("hld", "1.0", old_dir)
deb("hld", "1.1", repo)                                       # apt-mark hold
deb("prov", "1.0", old_dir)
deb("prov", "2.0", repo, "Depends: virt\n")                   # satisfied by a Provides
deb("provider", "1.0", old_dir, "Provides: virt\n")
deb("pha", "1.0", old_dir)
deb("pha", "1.1", repo)                                       # phased at 0 %
deb("baz", "1.0", old_dir)
deb("baz", "1.1", repo)                                       # plain update

run("sh", "-c", f"dpkg -i {old_dir}/*.deb")
run("apt-mark", "hold", "hld")
packages = run("apt-ftparchive", "packages", ".", cwd=repo)
packages = "\n\n".join(b + "\nPhased-Update-Percentage: 0" if b.startswith("Package: pha\n") else b
                       for b in packages.split("\n\n"))
with open(os.path.join(repo, "Packages"), "w") as f:
    f.write(packages)
with open("/etc/apt/sources.list.d/dsd-test.list", "w") as f:
    f.write(f"deb [trusted=yes] file:{repo} ./\n")
# Phasing only applies on a machine with an id
if not os.path.exists("/etc/machine-id") or not open("/etc/machine-id").read().strip():
    with open("/etc/machine-id", "w") as f:
        f.write("0123456789abcdef0123456789abcdef\n")
run("apt-get", "update", "-o", "Dir::Etc::sourcelist=/etc/apt/sources.list.d/dsd-test.list",
    "-o", "Dir::Etc::sourceparts=-")

ours = {"qs", "foo", "brk", "hld", "prov", "pha", "baz"}
held = {name: reason for name, reason in pkg_backend.holds().items() if name in ours}

print("Redenen")
check("qs, vastgepind door shellpkg", held.get("qs"), "kept back (via shellpkg)")
check("foo, wil een nieuw pakket", held.get("foo"), "kept back (needs new packages)")
check("brk, breekt victim", held.get("brk"), "kept back (via victim)")
check("hld, apt-mark hold", held.get("hld"), "hold")
check("pha, gefaseerd", held.get("pha"), "kept back (phased update)")
check("prov, afhankelijkheid via Provides is geen blokkade", "prov" in held, False)
check("baz, gewone update", "baz" in held, False)

print("Gelijk aan apt-get")
sim = run("apt-get", "-s", "upgrade", env=dict(os.environ, LC_ALL="C"))
kept, section = set(), None
for line in sim.splitlines():
    if line.startswith("The following"):
        section = "kept" if ("kept back" in line or "deferred" in line) else None
    elif line.startswith("  ") and section:
        kept.update(line.split())
    else:
        section = None
check("wat apt-get tegenhoudt is precies wat we vasthouden", kept & ours, set(held))

if failures:
    print(f"\n{len(failures)} mislukt")
    sys.exit(1)
print("\nalles ok")
