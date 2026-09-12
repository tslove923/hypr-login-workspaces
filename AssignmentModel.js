// Pure display/validation helpers for the Login Workspace Assignments panel.
// No file IO here -- the Python helper (bin/hlw-helper.py) owns all reading,
// writing, and security-sensitive validation. These are UI-side conveniences
// only; the helper re-validates everything independently and is the actual
// security boundary.

.pragma library

// Mirrors the Python helper's LABEL_RE, for instant inline feedback only.
var LABEL_RE = /^[\w][\w .:'()\/-]{0,63}$/

function isValidLabel(s) {
  return typeof s === "string" && LABEL_RE.test(s) && s.length <= 64
}

function destinationLabel(workspace) {
  if (!workspace) return ""
  if (workspace.type === "scratchpad") return "Scratchpad"
  if (workspace.type === "number") return "Workspace " + workspace.value
  return ""
}

function matchLabel(match) {
  if (!match) return ""
  var parts = []
  if (match.class) parts.push("class: " + match.class)
  if (match.title) parts.push("title: " + match.title)
  return parts.join(", ")
}

function toDisplayRow(assignment) {
  return {
    id: assignment.id,
    label: assignment.label,
    destination: destinationLabel(assignment.workspace),
    matchInfo: matchLabel(assignment.match),
    detected: !!assignment.detected
  }
}

// Inverse of the host's Util.fileUrl() (shell/Commons/Util.qml): turns a
// percent-encoded file:// URL (as produced by Qt.resolvedUrl against this
// QML file's own location) back into a plain filesystem path. Used instead
// of manifest.__sourceDir, which the host deliberately strips before handing
// a manifest to a third-party plugin -- this file-relative resolution needs
// no host-injected property at all.
function urlToLocalPath(url) {
  var s = String(url)
  if (s.indexOf("file://") === 0) s = s.slice(7)
  return s.split("/").map(decodeURIComponent).join("/")
}

function newId() {
  // Display-only placeholder before the helper assigns the real id on save;
  // never persisted or trusted as the actual identity.
  var chars = "0123456789abcdef"
  var out = ""
  for (var i = 0; i < 8; i++) out += chars.charAt(Math.floor(Math.random() * 16))
  return out
}
