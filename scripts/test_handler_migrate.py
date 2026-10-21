#!/usr/bin/env python3
"""Checks that the .appimage association survives the rename.

As Dank Software Depot the desktop entry was com.danklinux.dankSoftwareDepot
.desktop; as Dux Software Depot it is io.github.vinceecniv.DuxSoftwareDepot
.desktop. mimeapps.list names the file, so a user who had made the depot
the default for AppImages would silently lose that with the old entry gone.
--handler-migrate renames it in place; --handler-clear takes either away.

Run from anywhere:  python3 scripts/test_handler_migrate.py
"""
import io
import os
import sys
import tempfile
from contextlib import redirect_stdout

home = tempfile.mkdtemp()
os.environ["HOME"] = home
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import appimage  # noqa: E402  (MIMEAPPS is resolved against $HOME on import)

failures = []
OLD, NEW = appimage.LEGACY_DESKTOP, appimage.DEPOT_DESKTOP


def check(label, got, want):
    ok = got == want
    print(("ok   " if ok else "FOUT ") + label + ("" if ok else f": {got!r} != {want!r}"))
    if not ok:
        failures.append(label)


def write(text):
    os.makedirs(os.path.dirname(appimage.MIMEAPPS), exist_ok=True)
    with open(appimage.MIMEAPPS, "w") as f:
        f.write(text)


def read():
    with open(appimage.MIMEAPPS) as f:
        return f.read()


def run(fn):
    with redirect_stdout(io.StringIO()):
        fn()


def line(section, mime):
    return appimage._read_mimeapps().get(section, mime, fallback="")


print("Standaard blijft standaard")
write(f"""[Default Applications]
application/vnd.appimage={OLD};
application/x-iso9660-appimage={OLD};gearlever.desktop;
text/plain=gedit.desktop;

[Added Associations]
application/vnd.appimage=gearlever.desktop;{OLD};
""")
run(appimage.run_handler_migrate)
check("vnd.appimage wijst naar de nieuwe naam",
      line("Default Applications", "application/vnd.appimage"), f"{NEW};")
check("een tweede handler op dezelfde regel blijft, op zijn plek",
      line("Default Applications", "application/x-iso9660-appimage"), f"{NEW};gearlever.desktop;")
check("in Added Associations blijft de volgorde",
      line("Added Associations", "application/vnd.appimage"), f"gearlever.desktop;{NEW};")
check("wat niet van ons is, blijft onaangeroerd",
      line("Default Applications", "text/plain"), "gedit.desktop;")

print("Oud en nieuw allebei aanwezig")
write(f"[Default Applications]\napplication/vnd.appimage={NEW};{OLD};\n")
run(appimage.run_handler_migrate)
check("geen dubbele vermelding", line("Default Applications", "application/vnd.appimage"), f"{NEW};")

print("Niets om te migreren")
before = "[Default Applications]\napplication/vnd.appimage=gearlever.desktop;\n"
write(before)
run(appimage.run_handler_migrate)
check("bestand niet herschreven", read(), before)

print("Uitzetten na de rename")
write(f"[Default Applications]\napplication/vnd.appimage={OLD};gearlever.desktop;\n")
run(appimage.run_handler_clear)
check("de oude naam gaat ook weg", line("Default Applications", "application/vnd.appimage"),
      "gearlever.desktop;")

if failures:
    print(f"\n{len(failures)} mislukt")
    sys.exit(1)
print("\nalles ok")
