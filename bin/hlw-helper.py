#!/usr/bin/python3
"""
hlw-helper.py -- state, app catalog, Lua generation, and window detection for
the "Login Workspace Assignments" Omarchy plugin.

Always invoked as an argv array (never through a shell string), normally as:
  ["/usr/bin/python3", "-I", "-S", "<pluginDir>/bin/hlw-helper.py", <subcommand>, ...]

Every subcommand prints exactly one JSON document to stdout and exits 0;
errors are reported INSIDE that JSON body (an {"ok": false, "error": ...}
shape), not via stderr or a non-zero exit, so callers have exactly one bounded
read path for both outcomes. The only exit(2) path is a programmer error
(unknown subcommand / malformed invocation), which prints nothing to stdout.

Stdlib only. No third-party imports.
"""
import configparser
import json
import os
import pwd
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Fixed configuration. Never derived from the environment.
# ---------------------------------------------------------------------------

PLUGIN_ID = "io.github.lovetr923.hypr-login-workspaces"
STATE_DIR_PARTS = [".local", "state", PLUGIN_ID]
STATE_FILE = "assignments.json"
HYPR_DIR_PARTS = [".config", "hypr"]
LUA_FILE = "hypr-login-workspaces.lua"
LUA_BAK_FILE = "hypr-login-workspaces.lua.bak"

MAX_STATE_BYTES = 65536
MAX_ASSIGNMENTS = 32
MAX_LABEL_BYTES = 64
MAX_ARGV_TOKENS = 8
MAX_ARGV_TOKEN_BYTES = 256

FIXED_PATH = "/usr/bin:/usr/local/bin:/bin"
DESKTOP_DIRS = [
    "/usr/share/applications",
    "/usr/local/share/applications",
    os.path.join(pwd.getpwuid(os.geteuid()).pw_dir, ".local", "share", "applications"),
    "/var/lib/flatpak/exports/share/applications",
]
MAX_DESKTOP_FILES_VISITED = 2000
MAX_DESKTOP_ENTRIES_RETURNED = 300
MAX_DESKTOP_FILE_BYTES = 65536

DETECT_DEADLINE_S = 8.0
DETECT_POLL_INTERVAL_S = 0.3
DETECT_LAUNCH_TIMEOUT_S = 3.0
DETECT_OUTPUT_CAP = 262144

RELOAD_SCRIPT = "hlw-reload.sh"
WAIT_LAUNCH_SCRIPT = "hlw-wait-and-launch.sh"
OMAMAIL_DESKTOP_ID = "omamail.desktop"
OMAMAIL_MAILTO_REL = (".config", "omarchy", "plugins", "omamail", "scripts", "mailto.sh")

LABEL_RE = re.compile(r"^[\w][\w .:'()/-]{0,63}$", re.UNICODE)
ARGV_TOKEN_RE = re.compile(r"^[A-Za-z0-9_./:=@%+-]{1,256}$")
ID_RE = re.compile(r"^[0-9a-f]{8}$")

BUILTIN_COMMANDS = [
    {
        "id": "builtin:herdr",
        "name": "Herdr (terminal workspace manager)",
        "argv": ["omarchy-launch-terminal-herdr"],
        "class": None,
        "requiresDetect": True,
        "matchStrategy": "herdr-title-prefix",
    },
    {
        "id": "builtin:spotify",
        "name": "Spotify (Omarchy launcher)",
        "argv": ["omarchy-launch-spotify"],
        "class": "spotify",
        "requiresDetect": False,
    },
]

FIELD_CODE_RE = re.compile(r"%[fFuUick dDnNvm%]")


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Descriptor-bound directory/file primitives.
# ---------------------------------------------------------------------------

def open_dir_chain(parts, force_leaf_mode):
    """Walk from the passwd home with held descriptors. Returns an open dirfd
    for the final component. `force_leaf_mode`: chmod the final component to
    0700 if it is wider than that (only appropriate for a directory this
    plugin owns exclusively -- never for a shared directory like ~/.config/hypr)."""
    home = pwd.getpwuid(os.geteuid()).pw_dir
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for i, name in enumerate(parts):
            try:
                nfd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                nfd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nfd
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
                raise PermissionError("refusing untrusted directory component: %s" % name)
            if force_leaf_mode and i == len(parts) - 1 and st.st_mode & 0o077:
                os.fchmod(fd, 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


def repair_state_dir(dirfd):
    """Unconditional repair of the plugin's OWN state directory: remove any
    non-regular entry, lock every regular file to 0600. Never run this
    against a shared directory (e.g. ~/.config/hypr) -- only the leaf
    directory this plugin exclusively owns."""
    for name in os.listdir(dirfd):
        try:
            lst = os.lstat(name, dir_fd=dirfd, follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISREG(lst.st_mode):
            try:
                os.unlink(name, dir_fd=dirfd)
            except IsADirectoryError:
                pass
            except OSError:
                pass
            continue
        if lst.st_mode & 0o077:
            try:
                os.chmod(name, 0o600, dir_fd=dirfd, follow_symlinks=False)
            except OSError:
                pass


def read_bounded(dirfd, name, max_bytes):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dirfd)
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_nlink != 1:
            raise PermissionError("refusing to read %s: not an owned regular file" % name)
        if st.st_size > max_bytes:
            raise PermissionError("refusing to read %s: exceeds size limit" % name)
        os.set_blocking(fd, True)
        data = b""
        while len(data) <= max_bytes:
            chunk = os.read(fd, min(65536, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data += chunk
        if len(data) > max_bytes:
            raise PermissionError("refusing to read %s: grew past the limit" % name)
        return data
    finally:
        os.close(fd)


def read_bounded_shared(dirfd, name, max_bytes):
    """Like read_bounded, but for files this plugin does not own and never
    writes (system .desktop files) -- still O_NOFOLLOW|O_NONBLOCK against
    symlink/FIFO tricks and regular-file/size bounded, but with no ownership
    requirement, since these are legitimately root- or package-owned."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dirfd)
    except (FileNotFoundError, OSError):
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            return None
        os.set_blocking(fd, True)
        data = b""
        while len(data) <= max_bytes:
            chunk = os.read(fd, min(65536, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data += chunk
        if len(data) > max_bytes:
            return None
        return data
    finally:
        os.close(fd)


def write_atomic(dirfd, name, data: bytes):
    tmp = ".%s.%s.tmp" % (name, secrets.token_hex(8))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dirfd)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]
        os.fsync(fd)
        os.rename(tmp, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
        os.fsync(dirfd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dirfd)
        except OSError:
            pass
        raise
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Argv / executable resolution -- never consults os.environ.
# ---------------------------------------------------------------------------

def resolve_argv(tokens):
    """Validate every token's charset; resolve tokens[0] to an existing
    regular file via a fixed PATH (or accept it as-is if already absolute
    and it stat()s as a regular file). Returns the resolved argv list, or
    None to signal "reject this command, don't guess"."""
    if not tokens or len(tokens) > MAX_ARGV_TOKENS:
        return None
    for t in tokens:
        if not ARGV_TOKEN_RE.match(t):
            return None

    first = tokens[0]
    resolved = None
    if first.startswith("/"):
        try:
            st = os.stat(first, follow_symlinks=True)
            if stat.S_ISREG(st.st_mode):
                resolved = first
        except OSError:
            resolved = None
    else:
        found = shutil.which(first, path=FIXED_PATH)
        if found:
            resolved = found

    if resolved is None:
        return None
    return [resolved] + list(tokens[1:])


# ---------------------------------------------------------------------------
# .desktop catalog.
# ---------------------------------------------------------------------------

def _strip_field_codes(exec_line):
    return FIELD_CODE_RE.sub("", exec_line).strip()


def scan_desktop_entries():
    entries = []
    visited = 0
    seen_keys = set()

    for base in DESKTOP_DIRS:
        try:
            dirfd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            try:
                names = os.listdir(dirfd)
            except OSError:
                continue
            for name in sorted(names):
                if visited >= MAX_DESKTOP_FILES_VISITED or len(entries) >= MAX_DESKTOP_ENTRIES_RETURNED:
                    break
                if not name.endswith(".desktop"):
                    continue
                visited += 1
                raw = read_bounded_shared(dirfd, name, MAX_DESKTOP_FILE_BYTES)
                if raw is None:
                    continue
                try:
                    text = raw.decode("utf-8", "strict")
                except UnicodeDecodeError:
                    continue

                cp = configparser.ConfigParser(interpolation=None, strict=False)
                try:
                    cp.read_string(text)
                except configparser.Error:
                    continue
                if "Desktop Entry" not in cp:
                    continue
                section = cp["Desktop Entry"]

                if section.get("Type", "Application") != "Application":
                    continue
                if section.get("NoDisplay", "false").strip().lower() == "true":
                    continue
                if section.get("Hidden", "false").strip().lower() == "true":
                    continue

                exec_line = section.get("Exec", "")
                if not exec_line:
                    continue
                try:
                    tokens = shlex.split(_strip_field_codes(exec_line))
                except ValueError:
                    continue
                resolved = resolve_argv(tokens)
                if resolved is None:
                    continue

                display_name = section.get("Name", name)
                if not LABEL_RE.match(display_name):
                    display_name = display_name[:MAX_LABEL_BYTES] or name

                wm_class = section.get("StartupWMClass", "").strip() or None
                key = (display_name, resolved[0])
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                entries.append({
                    "desktopId": name,
                    "name": display_name,
                    "argv": resolved,
                    "class": wm_class,
                    "requiresDetect": wm_class is None,
                })
                if len(entries) >= MAX_DESKTOP_ENTRIES_RETURNED:
                    break
        finally:
            os.close(dirfd)
        if len(entries) >= MAX_DESKTOP_ENTRIES_RETURNED:
            break

    return entries


def cmd_list_apps():
    apps = []
    for b in BUILTIN_COMMANDS:
        resolved = resolve_argv(b["argv"])
        if resolved is None:
            continue
        apps.append({
            "desktopId": b["id"],
            "name": b["name"],
            "argv": resolved,
            "class": b.get("class"),
            "requiresDetect": b["requiresDetect"],
        })
    apps.extend(scan_desktop_entries())
    return {"ok": True, "apps": apps[:MAX_DESKTOP_ENTRIES_RETURNED + len(BUILTIN_COMMANDS)]}


# ---------------------------------------------------------------------------
# State read / validate / save.
# ---------------------------------------------------------------------------

def cmd_read_state():
    dirfd = open_dir_chain(STATE_DIR_PARTS, force_leaf_mode=True)
    try:
        raw = read_bounded(dirfd, STATE_FILE, MAX_STATE_BYTES)
        if raw is None:
            return {"ok": True, "assignments": []}
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"ok": False, "error": "state-corrupt", "assignments": []}
        assignments = doc.get("assignments", []) if isinstance(doc, dict) else []
        return {"ok": True, "assignments": assignments}
    finally:
        os.close(dirfd)


def _valid_workspace(ws):
    if not isinstance(ws, dict):
        return False
    if ws.get("type") == "scratchpad":
        return True
    if ws.get("type") == "number":
        v = ws.get("value")
        return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 10
    return False


def validate_assignment(obj):
    if not isinstance(obj, dict):
        return False, "not an object"
    if not ID_RE.match(str(obj.get("id", ""))):
        return False, "bad id"
    label = obj.get("label", "")
    if not isinstance(label, str) or not LABEL_RE.match(label) or len(label.encode("utf-8")) > MAX_LABEL_BYTES:
        return False, "bad label"
    if obj.get("source") not in ("desktop", "builtin", "custom"):
        return False, "bad source"
    argv = obj.get("argv")
    if not isinstance(argv, list) or not argv or len(argv) > MAX_ARGV_TOKENS:
        return False, "bad argv"
    for t in argv:
        if not isinstance(t, str) or not ARGV_TOKEN_RE.match(t) or len(t.encode("utf-8")) > MAX_ARGV_TOKEN_BYTES:
            return False, "bad argv token"
    match = obj.get("match")
    if not isinstance(match, dict) or not match:
        return False, "bad match"
    for k, v in match.items():
        if k not in ("class", "title") or not isinstance(v, str) or not v or len(v) > 128:
            return False, "bad match value"
        if any(ord(c) < 0x20 for c in v):
            return False, "bad match value"
    strategy = obj.get("matchStrategy", "exact")
    if strategy not in ("exact", "herdr-title-prefix"):
        return False, "bad matchStrategy"
    if strategy == "herdr-title-prefix" and "title" not in match:
        return False, "herdr-title-prefix requires a detected title"
    if not _valid_workspace(obj.get("workspace")):
        return False, "bad workspace"
    if not isinstance(obj.get("detected"), bool):
        return False, "bad detected flag"
    return True, ""


def cmd_save(payload: bytes):
    if len(payload) > MAX_STATE_BYTES:
        return {"ok": False, "error": "payload-too-large"}
    try:
        doc = json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "invalid-json"}
    if not isinstance(doc, dict):
        return {"ok": False, "error": "invalid-shape"}
    assignments = doc.get("assignments", [])
    if not isinstance(assignments, list) or len(assignments) > MAX_ASSIGNMENTS:
        return {"ok": False, "error": "too-many-assignments"}

    normalized = []
    for a in assignments:
        ok, why = validate_assignment(a)
        if not ok:
            return {"ok": False, "error": "invalid-assignment", "detail": why}
        row = {
            "id": a["id"],
            "label": a["label"],
            "source": a["source"],
            "desktopId": a.get("desktopId"),
            "argv": a["argv"],
            "match": a["match"],
            "matchStrategy": a.get("matchStrategy", "exact"),
            "workspace": a["workspace"],
            "detected": a["detected"],
            "createdAt": a.get("createdAt") or now_iso(),
            "updatedAt": now_iso(),
        }
        normalized.append(row)

    state_dirfd = open_dir_chain(STATE_DIR_PARTS, force_leaf_mode=True)
    try:
        repair_state_dir(state_dirfd)
        state_bytes = json.dumps({"schemaVersion": 1, "assignments": normalized}, indent=2).encode("utf-8") + b"\n"
        if len(state_bytes) > MAX_STATE_BYTES:
            return {"ok": False, "error": "state-too-large"}
        write_atomic(state_dirfd, STATE_FILE, state_bytes)
    finally:
        os.close(state_dirfd)

    hypr_dirfd = open_dir_chain(HYPR_DIR_PARTS, force_leaf_mode=False)
    try:
        old_lua = read_bounded(hypr_dirfd, LUA_FILE, MAX_STATE_BYTES) or b""
        new_lua = generate_lua(normalized).encode("utf-8")

        if new_lua != old_lua:
            if old_lua:
                write_atomic(hypr_dirfd, LUA_BAK_FILE, old_lua)
            write_atomic(hypr_dirfd, LUA_FILE, new_lua)

        reload_result = run_reload()
        if reload_result.startswith("ERROR"):
            if old_lua:
                write_atomic(hypr_dirfd, LUA_FILE, old_lua)
                run_reload()
            return {
                "ok": False,
                "error": "hyprland-config-error",
                "detail": reload_result[len("ERROR:"):][:500],
                "rolledBack": bool(old_lua),
            }
    finally:
        os.close(hypr_dirfd)

    return {"ok": True, "assignmentCount": len(normalized)}


# ---------------------------------------------------------------------------
# Lua generation.
# ---------------------------------------------------------------------------

def lua_escape(value: str) -> str:
    if any(ord(c) < 0x20 for c in value):
        raise ValueError("control byte in value destined for a Lua string literal")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def regex_literal(raw: str) -> str:
    """Anchor and escape a raw, already-validated identifier (a window class
    or an exact title) for use as a window-rule match pattern. Raw user text
    never reaches this function directly -- only values that came from a
    picked .desktop entry's own StartupWMClass or a live hyprctl-reported
    class/title via the detect-by-launch flow."""
    if not raw or len(raw) > 128 or any(ord(c) < 0x20 for c in raw):
        raise ValueError("unsuitable value for a match pattern")
    return "^" + re.escape(raw) + "$"


def herdr_title_pattern(detected_title: str) -> str:
    """Herdr's window title is '<hostname>: <session/workspace name>' by
    Omarchy's own shipped herdr config (window_title = "{hostname}: {workspace}").
    Anchor only the hostname prefix so renaming the session/workspace inside
    herdr doesn't break the match."""
    if ": " not in detected_title:
        raise ValueError("detected herdr title has no ': ' separator")
    hostname = detected_title.split(": ", 1)[0]
    if not hostname or len(hostname) > 128:
        raise ValueError("unsuitable herdr hostname prefix")
    return "^" + re.escape(hostname) + ": .+$"


def build_match_table(assignment) -> str:
    strategy = assignment.get("matchStrategy", "exact")
    match = assignment["match"]
    parts = []
    if strategy == "herdr-title-prefix":
        parts.append('title = "%s"' % lua_escape(herdr_title_pattern(match["title"])))
    else:
        for key in ("class", "title"):
            if key in match:
                parts.append('%s = "%s"' % (key, lua_escape(regex_literal(match[key]))))
    return "{ " + ", ".join(parts) + " }"


def shell_quote_single(token: str) -> str:
    return "'" + token.replace("'", "'\\''") + "'"


def workspace_value(ws) -> str:
    if ws.get("type") == "scratchpad":
        return "special:scratchpad silent"
    return "%d silent" % ws["value"]


def plugin_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_omamail_mailto():
    home = pwd.getpwuid(os.geteuid()).pw_dir
    path = os.path.join(home, *OMAMAIL_MAILTO_REL)
    try:
        st = os.stat(path, follow_symlinks=True)
        if stat.S_ISREG(st.st_mode) and os.access(path, os.X_OK):
            return path
    except OSError:
        pass
    return None


def launch_tokens_for(assignment):
    if assignment.get("desktopId") == OMAMAIL_DESKTOP_ID:
        wait_script = os.path.join(plugin_root(), "bin", WAIT_LAUNCH_SCRIPT)
        mailto = resolve_omamail_mailto()
        if mailto is None:
            return None
        return [wait_script, mailto]
    return assignment["argv"]


def generate_lua(assignments) -> str:
    lines = [
        "-- Generated by Login Workspace Assignments (%s).\n" % PLUGIN_ID,
        "-- Do not edit by hand -- edit through the plugin panel instead.\n",
        "-- Regenerated in full on every Save.\n",
        "\n",
    ]
    for a in assignments:
        tokens = launch_tokens_for(a)
        if tokens is None:
            lines.append("-- skipped %r: launch target not found on this machine\n" % a["label"])
            continue
        command = " ".join(shell_quote_single(t) for t in tokens)
        lines.append('o.launch_on_start("%s")\n' % lua_escape(command))

        try:
            match_table = build_match_table(a)
        except ValueError as e:
            lines.append("-- skipped window rule for %r: %s\n\n" % (a["label"], e))
            continue
        ws = lua_escape(workspace_value(a["workspace"]))
        lines.append('o.window(%s, { workspace = "%s" })\n' % (match_table, ws))
        lines.append("\n")
    return "".join(lines)


def run_reload():
    script = os.path.join(plugin_root(), "bin", RELOAD_SCRIPT)
    try:
        out = subprocess.run(
            [script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
            env={"PATH": FIXED_PATH},
        ).stdout
    except (subprocess.TimeoutExpired, OSError) as e:
        return "ERROR:reload script failed to run (%s)" % type(e).__name__
    text = out.decode("utf-8", "replace")[:600].splitlines()
    return text[0] if text else "ERROR:empty reload output"


# ---------------------------------------------------------------------------
# Detect-by-launch.
# ---------------------------------------------------------------------------

def _hyprctl_clients():
    try:
        proc = subprocess.Popen(
            ["/usr/bin/hyprctl", "clients", "-j"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={"PATH": FIXED_PATH},
        )
    except OSError:
        return []
    try:
        data = proc.stdout.read(DETECT_OUTPUT_CAP + 1)
        proc.wait(timeout=DETECT_LAUNCH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return []
    if len(data) > DETECT_OUTPUT_CAP:
        return []
    try:
        clients = json.loads(data.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return []
    if not isinstance(clients, list):
        return []
    out = []
    for c in clients[:500]:
        if not isinstance(c, dict):
            continue
        out.append({
            "address": str(c.get("address", ""))[:256],
            "class": str(c.get("class", ""))[:256],
            "title": str(c.get("title", ""))[:256],
        })
    return out


def cmd_detect_window(argv_tokens):
    resolved = resolve_argv(argv_tokens)
    if resolved is None:
        return {"ok": False, "error": "invalid-command"}

    before = {c["address"] for c in _hyprctl_clients()}

    env = {
        "HOME": pwd.getpwuid(os.geteuid()).pw_dir,
        "PATH": FIXED_PATH,
    }
    for keep in ("XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE"):
        if keep in os.environ:
            env[keep] = os.environ[keep]

    try:
        subprocess.Popen(resolved, start_new_session=True, close_fds=True, env=env)
    except OSError:
        return {"ok": False, "error": "launch-failed"}

    deadline = time.monotonic() + DETECT_DEADLINE_S
    while time.monotonic() < deadline:
        time.sleep(DETECT_POLL_INTERVAL_S)
        clients = _hyprctl_clients()
        fresh = [c for c in clients if c["address"] and c["address"] not in before]
        if len(fresh) == 1:
            c = fresh[0]
            return {"ok": True, "class": c["class"], "title": c["title"], "address": c["address"]}
        if len(fresh) > 1:
            return {"ok": False, "error": "ambiguous"}

    return {"ok": False, "error": "timeout"}


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------

def main(argv):
    if len(argv) < 2:
        return 2
    sub = argv[1]

    if sub == "list-apps":
        print(json.dumps(cmd_list_apps()))
        return 0

    if sub == "read-state":
        print(json.dumps(cmd_read_state()))
        return 0

    if sub == "save":
        # The payload is not secret (app names, workspace numbers, generated
        # match patterns -- no credentials), so it travels as a single argv
        # element rather than over stdin: simpler QML process lifecycle, with
        # no stdin-close timing to get right, and well inside ARG_MAX.
        if len(argv) < 3:
            return 2
        payload = argv[2].encode("utf-8")
        if len(payload) > MAX_STATE_BYTES:
            print(json.dumps({"ok": False, "error": "payload-too-large"}))
            return 0
        print(json.dumps(cmd_save(payload)))
        return 0

    if sub == "detect-window":
        tokens = argv[2:]
        if not tokens:
            return 2
        print(json.dumps(cmd_detect_window(tokens)))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
