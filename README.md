# Login Workspace Assignments

An [Omarchy](https://omarchy.org) Quickshell plugin: a graphical panel for picking installed
apps and assigning each one a Hyprland workspace (or the scratchpad) to launch on login.

## What it does

- Lets you pick an installed app (searched from your `.desktop` files) or one of two built-in
  launch targets (Herdr, Spotify).
- Lets you assign it a workspace number (1-10) or Hyprland's existing scratchpad
  (`special:scratchpad`, the same one already toggled by `Super+S` / `Super+grave` — this
  plugin never adds a new keybinding for it).
- For apps with no reliable window class in their `.desktop` file, offers a "detect by
  launching" flow: it launches the app once, watches for its new window, and shows you the
  detected class/title to confirm before saving.
- On Save, writes its own state file and regenerates its own Hyprland Lua file, then runs
  `hyprctl reload` and checks `hyprctl configerrors`. If Hyprland rejects the generated config,
  it automatically rolls back to the previous version.

## What it never does

This plugin **never edits** `~/.config/hypr/hyprland.lua`, `autostart.lua`, or `bindings.lua`.
It only ever writes two files it exclusively owns:

- `~/.local/state/io.github.tslove923.hypr-login-workspaces/assignments.json` — your saved
  assignments.
- `~/.config/hypr/hypr-login-workspaces.lua` — the generated `o.launch_on_start(...)` /
  `o.window(...)` rules. Regenerated in full on every Save; don't hand-edit it, edit through
  the panel instead. A `.bak` copy of the previous version is kept alongside it.

## One-time setup

After installing the plugin, add one line to your own `~/.config/hypr/hyprland.lua` (wherever
your other personal `require(...)` lines live) so Hyprland actually loads the generated file:

```lua
dofile(os.getenv("HOME") .. "/.config/hypr/hypr-login-workspaces.lua")
```

This is the only step the plugin asks you to do to a shared config file, and it does it for
you exactly once, by hand — the plugin will not add or modify this line itself.

## Opening the panel

```
omarchy-shell shell summon io.github.tslove923.hypr-login-workspaces '{}'
```

There is no bar icon; open it with the command above, or bind a key to it yourself, e.g. in
your own `bindings.lua`:

```lua
o.bind("SUPER + SHIFT + W", "Login workspace assignments",
  "omarchy-shell shell summon io.github.tslove923.hypr-login-workspaces '{}'")
```

## Known limitation: Herdr matching

Herdr (the terminal workspace manager) has no window class of its own — its window lands in
whatever terminal `xdg-terminal-exec` resolves to, which is the same class group every other
terminal window uses. So Herdr is matched by **window title** instead: Omarchy's own shipped
`herdr` config sets the title to `"<hostname>: <session>"`, and this plugin anchors only the
hostname prefix (`^<hostname>: .+$`), leaving the session/workspace name free. This means:

- Renaming a Herdr session changes only the part after the colon — the match still holds.
- Any other window whose title happens to start with the same `"<hostname>: "` prefix would
  also match. No other app installed alongside this plugin does that today, but it's a real,
  intentional trade-off, not an oversight.

## What every file written is for

| File | Written by | Contains |
|---|---|---|
| `~/.local/state/io.github.tslove923.hypr-login-workspaces/assignments.json` | Save | Your assignments: app, launch command, window match, workspace |
| `~/.config/hypr/hypr-login-workspaces.lua` | Save | Generated `o.launch_on_start`/`o.window` Lua, regenerated in full each time |
| `~/.config/hypr/hypr-login-workspaces.lua.bak` | Save | Previous version of the file above, kept for rollback |

No network access, no credentials, no telemetry. Every subprocess this plugin runs is a fixed,
argv-array command (never a shell string): `python3`, `hyprctl`, and the app you asked it to
launch or detect.

## Removing

```
omarchy plugin remove io.github.tslove923.hypr-login-workspaces
```

This removes the plugin itself. It does **not** delete:

- `~/.config/hypr/hypr-login-workspaces.lua` (and its `.bak`) — since the plugin never touched
  `hyprland.lua`, the `dofile(...)` line you added still points at a file that will keep
  existing with its last-saved rules. Delete both files and remove that line yourself if you
  want a clean removal.
- `~/.local/state/io.github.tslove923.hypr-login-workspaces/` — your saved assignments, in
  case you reinstall later. Delete it yourself if you don't want it kept.

## License

MIT — see `LICENSE`.
