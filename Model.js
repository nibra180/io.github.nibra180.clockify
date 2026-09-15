/* eslint-disable no-unused-vars */
// Pure helpers for the Clockify plugin: parsing the helper's JSON and turning
// seconds and entries into the strings the bar and the panel render. Kept out of
// the QML so the formatting rules live in one place and stay testable by eye.

// The helper always prints one JSON object, but a crashed interpreter or a
// missing python3 leaves us with noise -- treat anything unparseable as a
// failed refresh rather than letting the panel bind to undefined.
function parsePayload(raw) {
  var text = String(raw || "").trim()
  if (text === "") return { ok: false, error: "The Clockify helper returned nothing" }
  try {
    var payload = JSON.parse(text)
    if (!payload || typeof payload !== "object") throw new Error("not an object")
    return payload
  } catch (e) {
    return { ok: false, error: "Could not read the Clockify helper output" }
  }
}

function pad2(value) {
  return value < 10 ? "0" + value : String(value)
}

// H:MM:SS, the shape Clockify's own timer uses.
function formatClock(seconds) {
  var total = Math.max(0, Math.floor(Number(seconds) || 0))
  var hours = Math.floor(total / 3600)
  var minutes = Math.floor((total % 3600) / 60)
  return hours + ":" + pad2(minutes) + ":" + pad2(total % 60)
}

// "3h 12m" / "12m" / "0m" for totals, which read better than a clock.
function formatDuration(seconds) {
  var total = Math.max(0, Math.floor(Number(seconds) || 0))
  var hours = Math.floor(total / 3600)
  var minutes = Math.floor((total % 3600) / 60)
  if (hours > 0) return hours + "h " + pad2(minutes) + "m"
  return minutes + "m"
}

function parseStamp(value) {
  var text = String(value || "").trim()
  if (text === "") return null
  var millis = Date.parse(text)
  return Number.isNaN(millis) ? null : new Date(millis)
}

// Seconds the running entry has been going, measured against a clock the caller
// owns so the value re-evaluates on every tick.
function elapsedSeconds(startStamp, now) {
  var start = parseStamp(startStamp)
  if (!start || !now) return 0
  return Math.max(0, Math.floor((now.getTime() - start.getTime()) / 1000))
}

function describeEntry(entry) {
  if (!entry) return ""
  var description = String(entry.description || "").trim()
  return description === "" ? "No description" : description
}

function projectLabel(entry) {
  if (!entry) return ""
  var project = String(entry.projectName || "").trim()
  var client = String(entry.clientName || "").trim()
  if (project === "") return ""
  return client === "" ? project : project + " · " + client
}

function barTooltip(running, seconds, todaySeconds) {
  var today = "Today: " + formatDuration(todaySeconds)
  if (!running) return "Clockify · no timer running · " + today
  var project = projectLabel(running)
  var head = describeEntry(running) + (project === "" ? "" : " (" + project + ")")
  return head + " · " + formatClock(seconds) + " · " + today
}

function ticketNumber(value) {
  var match = String(value || "").trim().match(/^#(\d+)/)
  return match ? match[1] : ""
}

function ticketSuggestions(entries, input, limit) {
  var query = String(input || "").trim()
  var ticket = ticketNumber(query)
  if (ticket === "") return []

  var ticketPrefix = "#" + ticket
  var queryLower = query.toLowerCase()
  var preferred = []
  var other = []
  var list = Array.isArray(entries) ? entries : []

  for (var i = 0; i < list.length; i++) {
    var entry = list[i] || {}
    var description = String(entry.description || "").trim()
    if (!description.startsWith(ticketPrefix)) continue
    var next = description.charAt(ticketPrefix.length)
    if (next >= "0" && next <= "9") continue
    if (description.toLowerCase().startsWith(queryLower)) preferred.push(entry)
    else other.push(entry)
  }

  var maximum = Math.max(1, Number(limit) || 5)
  return preferred.concat(other).slice(0, maximum)
}

function rememberRecentEntry(entries, entry, limit) {
  var description = String((entry || {}).description || "").trim()
  if (ticketNumber(description) === "") return Array.isArray(entries) ? entries : []

  var remembered = [entry]
  var key = description.toLowerCase()
  var list = Array.isArray(entries) ? entries : []
  for (var i = 0; i < list.length; i++) {
    var existing = list[i] || {}
    if (String(existing.description || "").trim().toLowerCase() !== key)
      remembered.push(existing)
  }
  return remembered.slice(0, Math.max(1, Number(limit) || 100))
}

// Dropdown rows. The empty first option is how the user says "no project",
// which Clockify accepts and some workspaces require. `selectedId` keeps a
// remembered project from rendering as a raw id in the trigger during the
// moment between the panel opening and the project list arriving.
function projectOptions(projects, selectedId, allowNoProject) {
  var noProjectAllowed = allowNoProject !== false
  var options = noProjectAllowed
    ? [{ value: "", label: "No project", description: "" }]
    : []
  var list = Array.isArray(projects) ? projects : []
  var selected = String(selectedId || "")
  var matched = selected === "" && noProjectAllowed
  for (var i = 0; i < list.length; i++) {
    var project = list[i] || {}
    var id = String(project.id || "")
    if (id === selected) matched = true
    options.push({
      value: id,
      label: String(project.name || "Unnamed project"),
      description: String(project.clientName || "")
    })
  }
  if (!matched && selected !== "")
    options.push({ value: selected, label: "Selected project", description: "" })
  return options
}

// One-line status under the hero. Errors win over notes: a stale "Timer
// started" while the last refresh failed would be a lie.
function statusLine(error, note, configured) {
  if (String(error || "") !== "") return String(error)
  if (String(note || "") !== "") return String(note)
  if (!configured) return "Paste a Clockify API key to connect."
  return ""
}
