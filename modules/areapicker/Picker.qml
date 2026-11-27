pragma ComponentBehavior: Bound

import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import HelysIum
import HelysIum.I18n
import qs.components
import qs.components.effects
import qs.services
import qs.utils

MouseArea {
    id: root

    required property LazyLoader loader
    required property ShellScreen screen

    property bool onClient
    property int saveSeq: 0

    property real realBorderWidth: onClient ? (Hypr.options["general:border_size"] ?? 1) : 2
    property real realRounding: onClient ? (Hypr.options["decoration:rounding"] ?? 0) : 0

    property real ssx
    property real ssy

    property real sx: 0
    property real sy: 0
    property real ex: screen.width
    property real ey: screen.height

    property real rsx: Math.min(sx, ex)
    property real rsy: Math.min(sy, ey)
    property real sw: Math.abs(sx - ex)
    property real sh: Math.abs(sy - ey)

    property list<var> clients: {
        const mon = Hypr.monitorFor(screen);
        if (!mon)
            return [];

        const special = mon.lastIpcObject?.specialWorkspace;
        const wsId = special?.name ? special.id : mon.activeWorkspace?.id;
        if (wsId === undefined)
            return [];

        return Hypr.toplevelsForWs(wsId).sort((a, b) => {
            // Pinned first, then fullscreen, then floating, then any other
            const ac = a?.lastIpcObject;
            const bc = b?.lastIpcObject;
            if (!ac || !bc)
                return !ac - !bc; // Missing IPC last
            return (bc.pinned - ac.pinned) || ((bc.fullscreen !== 0) - (ac.fullscreen !== 0)) || (bc.floating - ac.floating);
        });
    }

    function checkClientRects(x: real, y: real): void {
        for (const client of clients) {
            if (!client)
                continue;

            const ipc = client.lastIpcObject;
            if (!ipc?.at || !ipc?.size)
                continue;

            let {
                at: [cx, cy],
                size: [cw, ch]
            } = ipc;
            cx -= screen.x;
            cy -= screen.y;
            if (cx <= x && cy <= y && cx + cw >= x && cy + ch >= y) {
                onClient = true;
                sx = cx;
                sy = cy;
                ex = cx + cw;
                ey = cy + ch;
                break;
            }
        }
    }

    function randomHex(): string {
        const a = Math.floor(Math.random() * 0x100000000).toString(16).padStart(8, "0");
        const b = Math.floor(Math.random() * 0x100000000).toString(16).padStart(8, "0");
        return `${a}${b}`;
    }

    function isSafeCacheDir(dirPath: string): bool {
        if (typeof dirPath !== "string" || dirPath.length === 0 || /[\x00-\x1f\x7f]/.test(dirPath))
            return false;
        const clean = dirPath.replace(/\/+$/, "");
        if (!clean.startsWith("/") || clean === "/" || clean === "/tmp" || clean.startsWith("/tmp/"))
            return false;
        if (/(^|\/)\.\.?(\/|$)/.test(clean))
            return false;
        return true;
    }

    function buildTempPath(cacheDir: string, pid: int): string {
        if (!root.isSafeCacheDir(cacheDir))
            return "";
        const clean = cacheDir.replace(/\/+$/, "");
        const safePid = (typeof pid === "number" && pid > 0) ? Math.floor(pid) : 0;
        const seq = ++root.saveSeq;
        return `${clean}/picker/picker-${safePid}-${Date.now()}-${seq}-${root.randomHex()}.png`;
    }

    function buildPostSaveCommand(path: string, clipboardOnly: bool, title: string, body: string): list<string> {
        if (typeof path !== "string" || !path.startsWith("/") || path.endsWith("/") || path.includes("//") || /[\x00-\x1f\x7f]/.test(path) || /(^|\/)\.\.?(\/|$)/.test(path))
            return [];
        const script = ["import os, signal, stat, subprocess, sys", "mode, path, title, body = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]", "dir_fd = -1", "fd = -1", "file_id = None", "name = ''", "def _sig(signum, _frame):", "    sys.exit(128 + signum)", "signal.signal(signal.SIGTERM, _sig)", "signal.signal(signal.SIGINT, _sig)", "try:", "    if not path.startswith('/') or path.endswith('/') or '//' in path:", "        sys.exit(1)", "    parts = [x for x in path.split('/') if x]", "    if len(parts) < 2 or '.' in parts or '..' in parts:", "        sys.exit(1)", "    name = parts[-1]", "    euid = os.geteuid()", "    dflags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)", "    dir_fd = os.open('/', dflags)", "    for seg in parts[:-1]:", "        next_fd = os.open(seg, dflags, dir_fd=dir_fd)", "        os.close(dir_fd)", "        dir_fd = next_fd", "        st_d = os.fstat(dir_fd)", "        if not stat.S_ISDIR(st_d.st_mode):", "            sys.exit(1)", "        if st_d.st_uid == euid and (st_d.st_mode & 0o002) != 0:", "            sys.exit(1)", "    st_p = os.fstat(dir_fd)", "    if st_p.st_uid != euid or (st_p.st_mode & 0o022) != 0:", "        sys.exit(1)", "    fflags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)", "    fd = os.open(name, fflags, dir_fd=dir_fd)", "    st = os.fstat(fd)", "    if not stat.S_ISREG(st.st_mode) or st.st_uid != euid or st.st_nlink != 1:", "        sys.exit(1)", "    file_id = (st.st_dev, st.st_ino)", "    if mode == 'clipboard':", "        res = subprocess.run(['wl-copy', '--type', 'image/png'], stdin=fd, check=False)", "        if res.returncode != 0:", "            sys.exit(res.returncode)", "        try:", "            subprocess.run(['notify-send', '-a', 'helysium-cli', f'--icon={path}', '--', title, body], check=False)", "        except OSError:", "            pass", "    else:", "        res = subprocess.run(['swappy', '-f', path], check=False)", "        if res.returncode != 0:", "            sys.exit(res.returncode)", "except OSError:", "    sys.exit(1)", "finally:", "    if fd >= 0:", "        try:", "            os.close(fd)", "        except OSError:", "            pass", "    if dir_fd >= 0 and file_id is not None and name:", "        try:", "            st_l = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)", "            if stat.S_ISREG(st_l.st_mode) and (st_l.st_dev, st_l.st_ino) == file_id:", "                os.unlink(name, dir_fd=dir_fd)", "        except OSError:", "            pass", "    if dir_fd >= 0:", "        try:", "            os.close(dir_fd)", "        except OSError:", "            pass"].join("\n");
        return ["python3", "-c", script, clipboardOnly ? "clipboard" : "swappy", path, title || "", body || ""];
    }

    function save(): void {
        const localTmp = root.buildTempPath(Paths.cache, Quickshell.processId);
        if (localTmp.length === 0) {
            closeAnim.start();
            return;
        }
        const tmpfile = Qt.resolvedUrl(localTmp);
        CUtils.saveItem(screencopy, tmpfile, Qt.rect(Math.ceil(rsx), Math.ceil(rsy), Math.floor(sw), Math.floor(sh)), path => {
            const cmd = root.buildPostSaveCommand(path, root.loader.clipboardOnly, Tr.tr("Screenshot taken"), Tr.tr("Screenshot copied to clipboard"));
            if (cmd.length > 0)
                Quickshell.execDetached(cmd);
            else
                CUtils.deleteFile(tmpfile);
            closeAnim.start();
        }, () => {
            CUtils.deleteFile(tmpfile);
            closeAnim.start();
        });
    }

    onClientsChanged: checkClientRects(mouseX, mouseY)

    anchors.fill: parent
    opacity: 0
    hoverEnabled: true
    cursorShape: Qt.CrossCursor

    Component.onCompleted: {
        Hypr.extras.refreshOptions();

        // Break binding if frozen
        if (loader.freeze)
            clients = clients;

        opacity = 1;

        const ipc = clients[0]?.lastIpcObject;
        if (ipc?.at && ipc?.size) {
            const cx = ipc.at[0] - screen.x;
            const cy = ipc.at[1] - screen.y;
            onClient = true;
            sx = cx;
            sy = cy;
            ex = cx + ipc.size[0];
            ey = cy + ipc.size[1];
        } else {
            sx = screen.width / 2 - 100;
            sy = screen.height / 2 - 100;
            ex = screen.width / 2 + 100;
            ey = screen.height / 2 + 100;
        }
    }

    onPressed: event => {
        ssx = event.x;
        ssy = event.y;
    }

    onReleased: {
        if (closeAnim.running)
            return;

        if (root.loader.freeze) {
            save();
        } else {
            overlay.visible = border.visible = false;
            screencopy.visible = false;
            screencopy.active = true;
        }
    }

    onPositionChanged: event => {
        const x = event.x;
        const y = event.y;

        if (pressed) {
            onClient = false;
            sx = ssx;
            sy = ssy;
            ex = x;
            ey = y;
        } else {
            checkClientRects(x, y);
        }
    }

    focus: true
    Keys.onEscapePressed: closeAnim.start()

    SequentialAnimation {
        id: closeAnim

        PropertyAction {
            target: root.loader
            property: "closing"
            value: true
        }
        ParallelAnimation {
            Anim {
                target: root
                property: "opacity"
                to: 0
                type: Anim.StandardLarge
            }
            Anim {
                target: root
                properties: "rsx,rsy"
                to: 0
            }
            Anim {
                target: root
                property: "sw"
                to: root.screen.width
            }
            Anim {
                target: root
                property: "sh"
                to: root.screen.height
            }
        }
        PropertyAction {
            target: root.loader
            property: "activeAsync"
            value: false
        }
    }

    Process {
        running: true
        command: ["hyprctl", "cursorpos", "-j"]
        stdout: StdioCollector {
            onStreamFinished: {
                const pos = JSON.parse(text);
                root.checkClientRects(pos.x - root.screen.x, pos.y - root.screen.y);
            }
        }
    }

    Loader {
        id: screencopy

        asynchronous: true
        anchors.fill: parent

        active: root.loader.freeze

        sourceComponent: ScreencopyView {
            captureSource: root.screen

            onHasContentChanged: {
                if (hasContent && !root.loader.freeze) {
                    overlay.visible = border.visible = true;
                    root.save();
                }
            }
        }
    }

    StyledRect {
        id: overlay

        anchors.fill: parent
        color: Colours.palette.m3secondaryContainer
        opacity: 0.3

        layer.enabled: true
        layer.effect: Mask {
            maskSource: selectionWrapper
            maskInverted: true
        }
    }

    Item {
        id: selectionWrapper

        anchors.fill: parent
        layer.enabled: true
        visible: false

        Rectangle {
            id: selectionRect

            radius: root.realRounding
            x: root.rsx
            y: root.rsy
            implicitWidth: root.sw
            implicitHeight: root.sh
        }
    }

    Rectangle {
        id: border

        color: "transparent"
        radius: root.realRounding > 0 ? root.realRounding + root.realBorderWidth : 0
        border.width: root.realBorderWidth
        border.color: Colours.palette.m3primary

        x: selectionRect.x - root.realBorderWidth
        y: selectionRect.y - root.realBorderWidth
        implicitWidth: selectionRect.implicitWidth + root.realBorderWidth * 2
        implicitHeight: selectionRect.implicitHeight + root.realBorderWidth * 2

        Behavior on border.color {
            CAnim {}
        }
    }

    Behavior on opacity {
        Anim {
            type: Anim.StandardLarge
        }
    }

    Behavior on rsx {
        enabled: !root.pressed

        Anim {}
    }

    Behavior on rsy {
        enabled: !root.pressed

        Anim {}
    }

    Behavior on sw {
        enabled: !root.pressed

        Anim {}
    }

    Behavior on sh {
        enabled: !root.pressed

        Anim {}
    }
}
