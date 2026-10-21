import QtQuick
import Quickshell.Io
import qs.Common
import qs.Services

// Takes the README screenshots: sets up one scene after another in the real
// window and grabs it. Driven by scripts/screenshots.py through the dev IPC
// target, on a working copy only.
//
// The window is shown in English whatever the machine speaks, and the
// Updates tab shows a recording rather than whatever happens to be pending —
// one built from the action log, so it is real packages this machine really
// updated. Everything else is the machine as it is. When it is done, or when
// it fails halfway, everything it changed is put back: the language, the
// simulation settings, the open tab.
//
// Each step runs, then waits until it has settled — at least `wait` ms, and
// until `until()` holds if it has one — and is grabbed if it has a name.
// grabToImage on the window's content takes the dialogs with it: they are
// items inside the window, not windows of their own.
Item {
    id: director

    property var window: null

    property bool running: false
    property string status: "idle"
    property var written: []
    // Steps that were still waiting when their time ran out — shot anyway,
    // and named in the status so a picture that looks wrong has a reason
    property var unsettled: []

    property var _opts: ({})
    property var _steps: []
    property int _index: -1
    property real _stepStarted: 0
    property var _saved: null

    readonly property string _langFile: "\"${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/dsd-lang\""

    // opts: {out, demo, empty, run, appimage, app}
    //   out       directory the raw shots are written to
    //   demo      recording whose packages fill the Updates tab
    //   empty     recording with no packages, for the up-to-date dashboard
    //   run       recording with streams, for the run in progress ("" skips it)
    //   appimage  an AppImage file to offer on the Install tab ("" skips it)
    //   app       the app whose details are shown, by name
    function start(opts) {
        if (running)
            return "already running: " + status;
        if (!window)
            return "no window";
        if (window.engine.running)
            return "an update run is going";
        _opts = opts;
        _saved = {
            lang: Tr.forcedLang,
            simMode: Backend.simMode,
            simRecording: Backend.simRecording,
            simSpeed: Backend.simSpeed
        };
        written = [];
        unsettled = [];
        _steps = _plan();
        _index = -1;
        running = true;
        status = "starting";
        _next();
        return "started: " + _steps.filter(s => s.name).length + " shots";
    }

    function _replay(name) {
        PluginService.savePluginData("dankSoftwareDepot", "simRecording", name);
        PluginService.savePluginData("dankSoftwareDepot", "simMode", "replay");
    }

    function _pendingCount() {
        return (window.pendingUpdates || []).length;
    }

    function _plan() {
        const win = window;
        const o = _opts;
        const steps = [];
        const settled = () => !window.store.loading;

        steps.push({
            run: () => {
                langProcess.command = ["sh", "-c", "printf en > " + _langFile];
                langProcess.running = true;
                Tr.forcedLang = "en";
                PluginService.savePluginData("dankSoftwareDepot", "simSpeed", 1);
                win.closeAllPopups();
                win.reloadTabs();
                win.activate();
                _replay(o.demo);
                win.openTab(0);
            },
            wait: 1500,
            until: () => Backend.simRecording === o.demo && (Backend.simPackages || []).length > 0 && _pendingCount() > 0 && settled()
        });
        // Enrichment starts on the package list changing, which happened
        // above; this waits for that answer, now in English
        steps.push({ name: "updates", run: () => window.store.refresh(window.pendingUpdates), wait: 2500, until: settled });
        steps.push({
            name: "security-changelog",
            run: () => {
                // The most severe advisory there is; without any, an app —
                // a package with its own icon reads better than a library
                const rows = win.updateRows || [];
                const rank = row => {
                    const adv = (window.widgetRoot ? window.widgetRoot.advisories : {})[window.store.stripArch(row.pkg.name)];
                    if (adv)
                        return adv.severity === "Critical" ? 4 : adv.severity === "Important" ? 3 : 2;
                    const info = window.store.infoFor(row.pkg);
                    return info && info.icon ? 1 : 0;
                };
                const row = rows.slice().sort((a, b) => rank(b) - rank(a))[0];
                if (row)
                    win.openUpdateDetails(row, window.store.infoFor(row.pkg));
            },
            wait: 3000,
            until: () => !win.updateDetailsLoading
        });
        steps.push({ run: () => { win.closeAllPopups(); _replay(o.empty); }, wait: 800, until: () => Backend.simRecording === o.empty && _pendingCount() === 0 });
        steps.push({ name: "updates-dashboard", run: () => win.openTab(0), wait: 2500 });
        steps.push({ name: "installed-library", run: () => win.openTab(1), wait: 5000 });
        steps.push({
            name: "install-storefront",
            run: () => win.openTab(2),
            wait: 6000,
            until: () => { const v = win.tabView(2); return v && (v.sections || []).length > 0; }
        });
        steps.push({
            name: "app-details",
            run: () => {
                const view = win.tabView(2);
                const entry = view ? view.showcaseEntry(o.app || "VLC") : null;
                if (entry)
                    view.openDetails(entry);
            },
            // Long enough for the screenshots in it to arrive too, which
            // load after the text
            wait: 6000,
            until: () => { const v = win.tabView(2); return v && !v.detailsLoading; }
        });
        if (o.appimage) {
            steps.push({ name: "appimage-install", run: () => { win.closeAllPopups(); win.tabView(2).offerAppimageFile(o.appimage); }, wait: 4000 });
        }
        steps.push({ name: "software-sources", run: () => { win.closeAllPopups(); win.openSourcesDialog(); }, wait: 4000 });
        steps.push({ name: "firmware-devices", run: () => { win.closeAllPopups(); win.openTab(3); }, wait: 4000 });
        steps.push({ name: "log-history", run: () => win.openTab(4), wait: 3000 });
        steps.push({ name: "plugin-settings", run: () => { win.openTab(0); win.settingsOpen = true; }, wait: 2000 });
        steps.push({ name: "command-palette", run: () => { win.closeAllPopups(); win.openPalette(); }, wait: 1500 });
        if (o.run) {
            steps.push({ run: () => { win.closeAllPopups(); _replay(o.run); }, wait: 800, until: () => Backend.simRecording === o.run && (Backend.simPackages || []).length > 0 });
            steps.push({
                run: () => {
                    PluginService.savePluginData("dankSoftwareDepot", "simSpeed", 4);
                    win.startSimulation();
                },
                wait: 1000,
                until: () => window.engine.running
            });
            // Far enough in for rows in every state: some done, one going,
            // the rest waiting
            steps.push({
                name: "update-in-progress",
                run: () => {},
                wait: 2000,
                until: () => {
                    const states = Object.values(window.engine.itemStates || {});
                    return states.filter(s => s.status === "done").length >= 3 && states.some(s => s.status === "active");
                }
            });
            steps.push({ run: () => window.engine.cancel(), wait: 1000, until: () => !window.engine.running });
        }
        return steps;
    }

    // What the conditions above look at, for a step that never settles
    function snapshot() {
        return "sim=" + Backend.simMode + "/" + Backend.simRecording
            + " simPackages=" + (Backend.simPackages || []).length
            + " pending=" + (window ? _pendingCount() : "-")
            + " loading=" + (window ? window.store.loading : "-")
            + " engine=" + (window ? window.engine.running : "-");
    }

    function _next() {
        _index++;
        if (_index >= _steps.length) {
            _finish("done: " + written.length + " shots" + (unsettled.length ? " (did not settle: " + unsettled.join("; ") + ")" : ""));
            return;
        }
        const step = _steps[_index];
        status = "step " + (_index + 1) + "/" + _steps.length + (step.name ? " " + step.name : "");
        try {
            step.run();
        } catch (e) {
            _finish("failed at step " + (_index + 1) + ": " + e);
            return;
        }
        _stepStarted = Date.now();
        settleTimer.restart();
    }

    Timer {
        id: settleTimer
        interval: 200
        repeat: true

        onTriggered: {
            const step = director._steps[director._index];
            const elapsed = Date.now() - director._stepStarted;
            if (elapsed < (step.wait || 0))
                return;
            let ready = true;
            try {
                ready = !step.until || step.until();
            } catch (e) {
                ready = false;
            }
            // A condition that never comes true is reported, not waited on
            // forever; the shot is still taken, and the status says why it
            // may be wrong
            const late = elapsed > (step.wait || 0) + 30000;
            if (!ready && !late)
                return;
            stop();
            if (late)
                console.warn("dankSoftwareDepot: screenshots: step", director._index + 1, "did not settle:", director.snapshot());
            if (late)
                director.unsettled = director.unsettled.concat([(step.name || "step " + (director._index + 1)) + ": " + director.snapshot()]);
            if (step.name)
                director._grab(step.name);
            else
                director._next();
        }
    }

    function _grab(name) {
        const path = _opts.out + "/" + name + ".png";
        const ok = window.grabWindow(result => {
            if (result.saveToFile(path))
                director.written = director.written.concat([path]);
            else
                console.warn("dankSoftwareDepot: screenshots: could not write", path);
            director._next();
        });
        if (!ok)
            _finish("failed: the window could not be grabbed (visible=" + window.visible + ")");
    }

    function _finish(message) {
        settleTimer.stop();
        running = false;
        status = message;
        const saved = _saved || {};
        langProcess.command = ["sh", "-c", "rm -f " + _langFile];
        langProcess.running = true;
        Tr.forcedLang = saved.lang || "";
        PluginService.savePluginData("dankSoftwareDepot", "simSpeed", saved.simSpeed !== undefined ? saved.simSpeed : 1);
        PluginService.savePluginData("dankSoftwareDepot", "simRecording", saved.simRecording || "");
        PluginService.savePluginData("dankSoftwareDepot", "simMode", saved.simMode || "");
        if (window) {
            window.closeAllPopups();
            window.reloadTabs();
            window.openTab(0);
        }
    }

    Process {
        id: langProcess
    }
}
