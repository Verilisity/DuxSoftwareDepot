#!/usr/bin/env python3
"""Checks pacman_helper.py against a stand-in for pyalpm.

This runs on a machine that has no pacman and no pyalpm, which is the point:
the Arch path is the one nobody here can try by hand, and it is where #13
happened — the transaction resolved against sync databases that nothing had
refreshed, so a run of eight updates reinstalled the eight versions already
installed and then reported that none of them had arrived.

The stand-in is a module on PYTHONPATH rather than a patched import, because
the helper re-execs itself looking for an interpreter that can import pyalpm
(see interp.ensure) and only a real, importable module stops it.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HELPER = Path(__file__).resolve().parent / "pacman_helper.py"

# name -> (installed version, version in the repositories before a refresh,
#          version in the repositories after one)
STUB = '''
import json, os

error = Exception
SCENARIO = json.loads(os.environ["PACMAN_STUB"])
TRACE = os.environ["PACMAN_TRACE"]


def _trace(what):
    with open(TRACE, "a") as fh:
        fh.write(what + "\\n")


def vercmp(a, b):
    return (a > b) - (a < b)


class Pkg:
    def __init__(self, name, version, arch="x86_64"):
        self.name = name
        self.version = version
        self.arch = arch
        self.download_size = 1000
        self.isize = 2000


class SyncDB:
    name = "extra"

    def __init__(self):
        # The refresh is pacman's, run before the handle opens: what it
        # wrote is what this database reads
        self.refreshed = os.path.exists(TRACE + ".refreshed")

    def update(self, force):
        _trace("pyalpm refresh")
        raise error("db.update is the path that crashed in #37")

    def get_pkg(self, name):
        entry = SCENARIO["packages"].get(name)
        if entry is None:
            return None
        version = entry["after"] if self.refreshed else entry["before"]
        _trace("resolve %s -> %s" % (name, version))
        return Pkg(name, version, entry.get("arch", "x86_64"))


class LocalDB:
    def get_pkg(self, name):
        entry = SCENARIO["packages"].get(name)
        if entry is None or entry.get("installed") is None:
            return None
        return Pkg(name, entry["installed"], entry.get("arch", "x86_64"))


class Transaction:
    def __init__(self, handle):
        self.handle = handle
        self.to_add = []
        self.to_remove = []

    def add_pkg(self, pkg):
        _trace("add %s-%s" % (pkg.name, pkg.version))
        self.to_add.append(pkg)

    def remove_pkg(self, pkg):
        self.to_remove.append(pkg)

    def prepare(self):
        pass

    def commit(self):
        # What pyalpm really passes its download callback: the event where
        # the bytes transferred used to be, and a pointer cut to an int
        # where the total was
        dlcb = getattr(self.handle, "dlcb", None)
        if dlcb:
            for pkg in self.to_add:
                filename = "%s-%s-x86_64.pkg.tar.zst" % (pkg.name, pkg.version)
                for event in (0, 1, 2, 1, 3):
                    dlcb(filename, event, -523683652)
        _trace("commit")

    def release(self):
        pass


class Handle:
    def __init__(self, root, dbpath):
        # A pyalpm linked against a libalpm it was not built for imports
        # cleanly and gives way here, at the first call into the library
        if SCENARIO.get("handleFails"):
            raise error("could not initialise the library")
        _trace("dbpath %s" % dbpath)
        # init_transaction takes db.lck here, which only root may create
        if os.path.realpath(dbpath) == os.path.realpath(os.environ["DSD_PACMAN_DBPATH"]):
            self._locked_out = True
        self._sync = [SyncDB()]
        # What pacman-conf says, set by the helper; empty means no check
        self.arch = []

    def register_syncdb(self, name, level):
        pass

    def get_syncdbs(self):
        return self._sync

    def get_localdb(self):
        return LocalDB()

    def init_transaction(self, **kwargs):
        if getattr(self, "_locked_out", False) and not os.environ.get("PACMAN_STUB_ROOT"):
            raise error("transaction could not be initialized, pm_errno 10 (unable to lock database)")
        return Transaction(self)

    # The helper installs progress callbacks when they exist; it checks with
    # hasattr, so leaving them off is a supported shape.
'''

# Stands in for /usr/bin/pacman, which now does the refresh (#37) and
# resolves the request before pyalpm is allowed to (the preflight)
FAKE_PACMAN = '''#!/usr/bin/env python3
import json, os, sys
scenario = json.loads(os.environ["PACMAN_STUB"])
trace = os.environ["PACMAN_TRACE"]
with open(trace, "a") as fh:
    fh.write("pacman " + " ".join(sys.argv[1:]) + "\\n")
if "--print" in sys.argv:
    with open(trace, "a") as fh:
        fh.write("preflight\\n")
    if scenario.get("preflightFails"):
        print("error: unresolvable package conflicts detected", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
if scenario.get("refreshFails"):
    print("error: failed retrieving file 'extra.db' from mirror.example : mirror unreachable", file=sys.stderr)
    sys.exit(1)
with open(trace, "a") as fh:
    fh.write("refresh\\n")
open(trace + ".refreshed", "w").close()
'''

# pacman-conf, when the scenario names the architectures the fake pacman
# accepts; a scenario without them has no pacman-conf at all, which is a
# machine where the question cannot be asked.
FAKE_PACMAN_CONF = '''#!/usr/bin/env python3
import os, sys
with open(os.environ["PACMAN_TRACE"], "a") as fh:
    fh.write("pacman-conf " + " ".join(sys.argv[1:]) + "\\n")
for arch in os.environ["PACMAN_ARCHS"].split():
    print(arch)
'''

# What the last run left behind, for the checks that look at the fixture
# rather than at the events
LAST = {}


def run(scenario, *args):
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "pyalpm.py").write_text(STUB)
        bindir = Path(tmp) / "bin"
        bindir.mkdir()
        (bindir / "pacman").write_text(FAKE_PACMAN)
        (bindir / "pacman").chmod(0o755)
        if scenario.get("architectures") is not None:
            (bindir / "pacman-conf").write_text(FAKE_PACMAN_CONF)
            (bindir / "pacman-conf").chmod(0o755)
        # Stands in for /var/lib/pacman: where the lock lives, and what a
        # plan must not open for a transaction
        dbpath = Path(tmp) / "db"
        dbpath.mkdir()
        trace = Path(tmp) / "trace"
        trace.write_text("")
        env = dict(os.environ)
        env["DSD_PACMAN_DBPATH"] = str(dbpath)
        if scenario.get("architectures") is not None:
            env["PACMAN_ARCHS"] = " ".join(scenario["architectures"])
        lock = dbpath / "db.lck"
        if scenario.get("staleLock") or scenario.get("heldLock"):
            lock.touch()
            # Old enough that it cannot be read as a run that is starting
            os.utime(lock, (0, 0))
            # A /proc of two processes: a bystander, and — when the lock is
            # held — the holder, with the lock among its open files the way
            # libalpm keeps it. A real /proc has processes the test user
            # may not look into, whose answer is "cannot tell"; the helper
            # running as root has no such processes.
            proc_dir = Path(tmp) / "proc"
            for pid, comm, argv, holds in ((4242, "sleep", "sleep 60", False),
                                           (4343, "pacman", "pacman -Syu", scenario.get("heldLock"))):
                if comm == "pacman" and not holds:
                    continue
                (proc_dir / str(pid) / "fd").mkdir(parents=True)
                (proc_dir / str(pid) / "comm").write_text(comm + "\n")
                (proc_dir / str(pid) / "cmdline").write_bytes(argv.replace(" ", "\0").encode() + b"\0")
                if holds:
                    os.symlink(str(lock), proc_dir / str(pid) / "fd" / "3")
            env["DSD_PROC"] = str(proc_dir)
        env["PATH"] = "%s:%s" % (bindir, env.get("PATH", ""))
        env["PYTHONPATH"] = tmp
        env["PACMAN_STUB"] = json.dumps(scenario)
        env["PACMAN_TRACE"] = str(trace)
        # Transactions run as root under pkexec; only `plan` and `selftest`
        # are the user's own
        if args[0] not in ("plan", "selftest"):
            env["PACMAN_STUB_ROOT"] = "1"
        proc = subprocess.run([sys.executable, str(HELPER), *args],
                              capture_output=True, text=True, env=env, timeout=60)
        events = []
        for line in proc.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        lines = [line for line in trace.read_text().splitlines()
                 if not line.startswith(("dbpath ", "pacman "))]
        LAST.clear()
        LAST["lockRemains"] = lock.exists()
        LAST["dbpath"] = str(dbpath)
        return events, lines, proc, trace.read_text().splitlines()


def event(events, kind):
    for item in events:
        if item.get("event") == kind:
            return item
    return None


def check(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label + (" — " + detail if detail and not condition else ""))
    return condition


def main():
    ok = True

    # The reported case: the repositories have a newer build, the sync
    # database on disk does not know it yet.
    behind = {"packages": {"dolphin": {"installed": "26.08.0-2",
                                       "before": "26.08.0-2",
                                       "after": "26.08.1-1"}}}
    events, trace, proc, _ = run(behind, "upgrade", "dolphin")
    ok &= check("refresh happens before the package is resolved",
                trace[:3] == ["refresh", "preflight", "resolve dolphin -> 26.08.1-1"], str(trace))
    plan = event(events, "plan")
    ok &= check("the plan carries the version the refresh revealed",
                plan is not None and plan["ops"][0]["evr"] == "26.08.1-1", json.dumps(plan))
    ok &= check("and it is an upgrade, not a reinstall",
                plan is not None and plan["ops"][0]["action"] == "Upgrade")
    ok &= check("the transaction is committed", "commit" in trace)
    downloads = [e for e in events if e.get("phase") == "download"]
    ok &= check("the download is reported with the plan's size, not the callback's pointer",
                bool(downloads) and all(e.get("bytesTotal") == 1000 for e in downloads if "bytesTotal" in e),
                json.dumps(downloads))
    ok &= check("and starts once, ends once, through a retry",
                [e["event"] for e in downloads] == ["op-start", "progress", "op-done"]
                and downloads[-1].get("totalTransferred") == 1000, json.dumps(downloads))
    events, trace, proc, raw = run(behind, "upgrade", "dolphin")
    ok &= check("pacman does the refresh, not pyalpm's db.update (#37)",
                "pacman --sync --refresh --noconfirm --noprogressbar" in raw
                and "pyalpm refresh" not in raw, str(raw))

    # Already at the version the repositories offer: adding it anyway is the
    # "is up to date -- reinstalling" line from the report.
    current = {"packages": {"libdeflate": {"installed": "1.25-1",
                                           "before": "1.25-1",
                                           "after": "1.25-1"}}}
    events, trace, proc, _ = run(current, "upgrade", "libdeflate")
    ok &= check("a package already at the repository version is not added",
                not any(line.startswith("add ") for line in trace), str(trace))
    done = event(events, "done")
    ok &= check("and the run says there was nothing to do",
                done is not None and done.get("ok") is True and done.get("nothingToDo") is True,
                json.dumps(done))

    # An unreachable mirror is not a reason to refuse the transaction: what
    # the database already knows about still gets installed.
    partly = {"refreshFails": True,
              "packages": {"dolphin": {"installed": "26.08.0-2",
                                       "before": "26.08.1-1",
                                       "after": "26.08.2-1"}}}
    events, trace, proc, _ = run(partly, "upgrade", "dolphin")
    ok &= check("a failed refresh still installs what the stale database has",
                "commit" in trace and "add dolphin-26.08.1-1" in trace, str(trace))
    ok &= check("and says why it could not refresh",
                any(e.get("event") == "error" and "refresh" in e.get("message", "") for e in events))

    # And when the stale database has nothing newer, the run has nothing it
    # can do — which must not read as "updated". The reason is the failed
    # refresh, and it has to reach the caller, because the check that follows
    # will otherwise fail every row with nothing to show for it.
    stuck = dict(behind)
    stuck["refreshFails"] = True
    events, trace, proc, _ = run(stuck, "upgrade", "dolphin")
    ok &= check("a stale database with nothing newer adds nothing",
                not any(line.startswith("add ") for line in trace), str(trace))
    ok &= check("and the refresh failure is the reason on the wire",
                any(e.get("event") == "error" and "mirror unreachable" in e.get("message", "") for e in events))

    # `plan` is unprivileged: writing the sync databases needs root.
    events, trace, proc, raw = run(behind, "plan", "upgrade", "dolphin")
    ok &= check("plan does not try to refresh",
                not any("refresh" in line for line in raw), str(raw))
    ok &= check("nor ask pacman to resolve", "preflight" not in trace, str(trace))
    ok &= check("and does not commit", "commit" not in trace)

    # The lock libalpm takes in init_transaction lives beside the databases,
    # and only root may create it in /var/lib/pacman: a plan opened there
    # failed every time (#38). It gets a directory of its own, removed after.
    done = event(events, "done")
    ok &= check("plan resolves without the lock on the real databases",
                done is not None and done.get("ok") is True
                and not any("unable to lock" in e.get("message", "") for e in events),
                json.dumps(events))
    dbpaths = [line[len("dbpath "):] for line in raw if line.startswith("dbpath ")]
    ok &= check("in a private database directory",
                bool(dbpaths) and os.path.realpath(dbpaths[-1]) != os.path.realpath(LAST["dbpath"]), str(raw))
    ok &= check("which is gone afterwards",
                bool(dbpaths) and not os.path.exists(dbpaths[-1]), dbpaths[-1] if dbpaths else "")

    # Installing is the other side of the same staleness: a package added to
    # the repositories today is not in yesterday's database at all.
    fresh = {"packages": {"newthing": {"installed": None,
                                       "before": None,
                                       "after": "1.0-1"}}}
    events, trace, proc, _ = run(fresh, "install", "newthing")
    ok &= check("install resolves against the refreshed database",
                any(line == "add newthing-1.0-1" for line in trace), str(trace))

    # An AUR-only package is absent from every sync database by definition —
    # libalpm cannot resolve it, and the helper refuses the transaction with
    # the reason on the wire rather than a silent half-run. This is the
    # failure behind the "package not found" reports: the QML layer routes
    # repo "aur" updates to the DMS daemon's AUR backend (paru/yay) and
    # never hands them here. The test pins the refusal that routing exists
    # to avoid, so a future caller cannot reintroduce it by accident.
    events, trace, proc, _ = run(behind, "upgrade", "some-aur-package")
    ok &= check("a package outside every sync database fails with its reason",
                any(e.get("event") == "error" and "package not found: some-aur-package" in e.get("message", "") for e in events))
    done = event(events, "done")
    ok &= check("and the failure names the package",
                done is not None and done.get("ok") is False and "some-aur-package" in (done.get("failed") or []),
                json.dumps(done))

    # The startup check has to answer for the bindings the transaction will
    # actually use, not merely for the import. A pyalpm that imports and then
    # cannot open a handle used to pass the selftest, so the plugin said
    # nothing at startup and every later run failed with no reason to show
    # (#18); the selftest now takes that step itself.
    events, trace, proc, _ = run(behind, "selftest")
    done = event(events, "done")
    ok &= check("selftest passes when the library can be opened",
                proc.returncode == 0 and done is not None and done.get("ok") is True,
                json.dumps(done))

    events, trace, proc, _ = run({"packages": {}, "handleFails": True}, "selftest")
    ok &= check("selftest fails when pyalpm imports but libalpm will not open",
                proc.returncode != 0 and any(e.get("event") == "error" and "libalpm could not be used" in e.get("message", "") for e in events),
                proc.stdout + proc.stderr)
    ok &= check("and the reason libalpm gave travels with it",
                any("could not initialise the library" in e.get("message", "") for e in events if e.get("event") == "error"))

    # pacman resolves the request before pyalpm does, because pyalpm 0.12
    # segfaults on two of libalpm's three ways of refusing a transaction
    # (a conflict, a wrong architecture) and a helper dead inside its
    # transaction leaves the lock behind. A refusal from pacman is a reason
    # on the wire and no transaction at all.
    refused = dict(behind)
    refused["preflightFails"] = True
    events, trace, proc, _ = run(refused, "upgrade", "dolphin")
    ok &= check("a request pacman would refuse never reaches pyalpm",
                not any(line.startswith("add ") for line in trace) and "commit" not in trace, str(trace))
    ok &= check("and pacman's reason is the one reported",
                any(e.get("event") == "error" and "unresolvable package conflicts" in e.get("message", "") for e in events),
                json.dumps(events))

    # The architecture list is pacman-conf's, not pycman's: CachyOS's
    # pacman resolves `Architecture = auto` to the CPU's feature levels and
    # ships x86_64_v3 packages, which a handle that accepts only x86_64
    # refuses — through pyalpm, with a segfault, in a plan as in a run.
    v3 = {"architectures": ["x86_64", "x86_64_v2", "x86_64_v3"],
          "packages": {"libsoup3": {"installed": "3.8.0-1", "before": "3.8.1-1.1",
                                    "after": "3.8.1-1.1", "arch": "x86_64_v3"}}}
    events, trace, proc, raw = run(v3, "upgrade", "libsoup3")
    ok &= check("the accepted architectures are asked of pacman-conf",
                "pacman-conf Architecture" in raw, str(raw))
    ok &= check("and a package of an accepted level installs",
                "add libsoup3-3.8.1-1.1" in trace and "commit" in trace, str(trace))
    events, trace, proc, raw = run(v3, "plan", "upgrade", "libsoup3")
    ok &= check("a plan asks the same question",
                "pacman-conf Architecture" in raw and event(events, "plan") is not None, str(raw))
    narrow = dict(v3)
    narrow["architectures"] = ["x86_64"]
    events, trace, proc, _ = run(narrow, "upgrade", "libsoup3")
    ok &= check("a package of a level this pacman does not accept is refused before prepare()",
                not any(line.startswith("add ") for line in trace) and "commit" not in trace, str(trace))
    ok &= check("with the architecture named",
                any(e.get("event") == "error" and "architecture is not valid: libsoup3-3.8.1-1.1-x86_64_v3" in e.get("message", "") for e in events),
                json.dumps(events))

    # A lock nothing holds is what a helper that died in its transaction
    # leaves behind, and what every run after it used to fail on. The
    # helper clears it, says so, and goes on; a lock something holds stays,
    # and the run names the holder instead of starting.
    stale = dict(behind)
    stale["staleLock"] = True
    events, trace, proc, _ = run(stale, "upgrade", "dolphin")
    ok &= check("a stale lock is removed and the run goes on",
                not LAST["lockRemains"] and "commit" in trace, str(trace) + " lockRemains=%s" % LAST["lockRemains"])
    ok &= check("and the removal is announced",
                any(e.get("event") == "warning" and "stale pacman lock" in e.get("message", "") for e in events),
                json.dumps(events))
    held = dict(behind)
    held["heldLock"] = True
    events, trace, proc, _ = run(held, "upgrade", "dolphin")
    ok &= check("a lock something holds is left alone",
                LAST["lockRemains"] and "commit" not in trace and "refresh" not in trace, str(trace))
    ok &= check("and the run says who has it",
                any(e.get("event") == "error" and "pacman is busy" in e.get("message", "") for e in events),
                json.dumps(events))

    print("\n" + ("all checks passed" if ok else "FAILURES"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
