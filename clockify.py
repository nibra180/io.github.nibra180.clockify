#!/usr/bin/env python3
"""Clockify bridge for the io.github.nibra180.clockify Omarchy plugin.

Every subcommand prints exactly one JSON object on stdout and exits 0, even
when Clockify refuses the call, so the QML side has a single shape to parse and
only has to look at `ok` / `error`. The API key is read here -- from a 0600
config file or $CLOCKIFY_API_KEY -- and never travels through argv, where `ps`
would show it to every user on the machine.

Usable by hand while developing the plugin:

    ./clockify.py status --projects | jq
    ./clockify.py start --description "Writing docs" --project 5f0...
    ./clockify.py stop
"""

from __future__ import annotations

import argparse
import fcntl
import http.client
import json
import os
import socket
import ssl
import sys
import tempfile
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic

HOST = "api.clockify.me"
API_PATH = "/api/v1"
USER_AGENT = "omarchy-clockify/1.0"
TIMEOUT = 6
# Ceiling for one call including its failover attempts: enough for every
# resolved address to get a turn, and no more. Without it, a handful of dead
# routes would leave the panel waiting on an answer that is not coming.
DEADLINE = 26
PAGE_SIZE = 200
PAGE_LIMIT = 5
HISTORY_LIMIT = 100
HISTORY_PAGE_LIMIT = 5


class ApiError(Exception):
    """A Clockify call that failed in a way worth showing the user."""

    def __init__(self, message, code=None, reason=""):
        super().__init__(message)
        self.code = code
        self.reason = reason


class Identity:
    """Who we are and which workspace we act in."""

    def __init__(self, user_id, workspace_id, user_name):
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.user_name = user_name


# ----------------------------------------------------------------- config

def config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "omarchy" / "clockify.json"


def load_config():
    try:
        with open(config_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@contextmanager
def config_lock():
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _save_config(config):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".clockify-", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(config, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def save_config(config):
    with config_lock():
        _save_config(config)


def update_config(changes=None, removals=(), reset_project_for_workspace=False):
    with config_lock():
        config = load_config()
        values = changes or {}
        if (
            reset_project_for_workspace
            and "workspaceId" in values
            and values["workspaceId"] != str(config.get("workspaceId") or "")
            and "defaultProjectId" not in values
        ):
            config.pop("defaultProjectId", None)
        config.update(values)
        for key in removals:
            config.pop(key, None)
        _save_config(config)
        return config


def environment_api_key():
    return str(os.environ.get("CLOCKIFY_API_KEY") or "").strip()


def api_key(config):
    return environment_api_key() or str(config.get("apiKey") or "").strip()


def identity_config(config):
    return {} if environment_api_key() else config


def default_project_id(config):
    return "" if environment_api_key() else str(config.get("defaultProjectId") or "")


# -------------------------------------------------------------------- http

class Api:
    """HTTP/1.1 client for one run of one subcommand.

    Deliberately not urllib: api.clockify.me resolves to several CloudFront
    addresses, and a network that cannot reach one of them (a filtered route, a
    broken path) gets a connection that opens and then answers nothing. urllib
    picks whatever address the resolver returns first and hangs there, which
    looks exactly like "Clockify is down" every other refresh. So we resolve the
    host ourselves, fail over to the next address when one stops answering, and
    keep the connection that worked for the rest of the run.
    """

    def __init__(self, key, preferred=""):
        self.key = key
        self.addresses = self._resolve()
        self.connection = None
        self.address = None
        # Host that answered on a previous run, so a network where half the
        # addresses black-hole does not re-discover that on every refresh. Only
        # honoured while the resolver still hands it out.
        self.preferred = str(preferred or "")
        self.good_host = ""

    @staticmethod
    def _resolve():
        try:
            infos = socket.getaddrinfo(HOST, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except OSError as error:
            raise ApiError("Could not resolve %s: %s" % (HOST, error)) from error
        ordered = []
        for info in infos:
            address = (info[0], info[4])
            if address not in ordered:
                ordered.append(address)
        if not ordered:
            raise ApiError("Could not resolve %s" % HOST)
        return ordered

    def _open(self, family, sockaddr):
        raw = socket.socket(family, socket.SOCK_STREAM)
        try:
            raw.settimeout(TIMEOUT)
            raw.connect(sockaddr)
            context = ssl.create_default_context()
            secure = context.wrap_socket(raw, server_hostname=HOST)
        except Exception:
            raw.close()
            raise
        # http.client does the HTTP framing; the socket is already connected and
        # wrapped, so hand it the finished one rather than let it dial again.
        connection = http.client.HTTPSConnection(HOST, 443, timeout=TIMEOUT)
        connection.sock = secure
        return connection

    def _close(self):
        if self.connection is not None:
            try:
                self.connection.close()
            except OSError:
                pass
        self.connection = None

    def call(self, method, path, body=None, params=None):
        target = API_PATH + path
        if params:
            target += "?" + urllib.parse.urlencode(params)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "X-Api-Key": self.key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

        # Order of attempts: the connection this run already holds, then the
        # address that worked on an earlier run, then everything else.
        candidates = []
        if self.address is not None:
            candidates.append(self.address)
        candidates.extend(a for a in self.addresses
                          if a != self.address and a[1][0] == self.preferred)
        candidates.extend(a for a in self.addresses
                          if a != self.address and a[1][0] != self.preferred)

        failure = None
        retryable = method.upper() in {"GET", "HEAD", "OPTIONS"}
        expiry = monotonic() + DEADLINE
        while candidates and monotonic() < expiry:
            candidate = candidates.pop(0)
            reused = self.connection is not None and self.address == candidate
            if not reused:
                self._close()
                try:
                    self.connection = self._open(*candidate)
                    self.address = candidate
                except (OSError, http.client.HTTPException) as error:
                    failure = error
                    continue
            connection = self.connection
            if connection is None:
                continue
            try:
                connection.request(method, target, payload, headers)
                response = connection.getresponse()
                raw = response.read().decode("utf-8", "replace")
            except OSError as error:
                failure = self._request_failure(error, retryable, reused, candidate, candidates)
                continue
            except http.client.HTTPException as error:
                failure = self._request_failure(error, retryable, reused, candidate, candidates)
                continue
            self.good_host = candidate[1][0]
            return self._decode(response.status, raw)

        raise ApiError("Clockify did not answer (%s)" % (describe(failure),))

    def _request_failure(self, error, retryable, reused, candidate, candidates):
        self._close()
        if not retryable:
            raise ApiError(
                "Clockify may have applied the change, but its response was lost; refresh before trying again"
            ) from error
        if reused and not is_timeout(error):
            candidates.insert(0, candidate)
        return error

    @staticmethod
    def _decode(status, raw):
        if status >= 400:
            raise ApiError(status_message(status, raw), status)
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError as error:
            raise ApiError("Could not parse the Clockify response") from error


def is_timeout(error):
    # socket.timeout is an alias of TimeoutError on 3.10+; both spellings kept
    # so this reads correctly wherever it is ported.
    return isinstance(error, (TimeoutError, socket.timeout))


def describe(error):
    if error is None:
        return "no route answered"
    if is_timeout(error):
        return "timed out after %ds" % TIMEOUT
    return str(error) or error.__class__.__name__


def status_message(status, raw):
    detail = ""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            detail = str(parsed.get("message") or "").strip()
    except ValueError:
        detail = ""
    if status == 401:
        return detail or "Clockify rejected the API key"
    if status == 403:
        return detail or "Clockify denied the request"
    if status == 429:
        return "Clockify rate limit reached, try again in a moment"
    return detail or ("Clockify returned HTTP %d" % status)


# --------------------------------------------------------------------- time

def now_utc():
    return datetime.now(timezone.utc)


def utc_stamp(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(value):
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def local_midnight(moment):
    return moment.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)


def week_start(moment, first_day):
    """Local midnight at the start of the current week."""
    today = local_midnight(moment)
    weekday = today.weekday()  # Monday is 0
    offset = (weekday + 1) % 7 if str(first_day).lower() == "sunday" else weekday
    return today - timedelta(days=offset)


# ------------------------------------------------------------------ reading

def identity(config, api):
    user = api.call("GET", "/user") or {}
    user_id = str(user.get("id") or "")
    workspace_id = str(
        config.get("workspaceId")
        or user.get("activeWorkspace")
        or user.get("defaultWorkspace")
        or ""
    )
    if not user_id or not workspace_id:
        raise ApiError("Clockify did not return a user and workspace")
    return Identity(user_id, workspace_id, str(user.get("name") or user.get("email") or ""))


def fetch_projects(workspace_id, api):
    projects = []
    for page in range(1, PAGE_LIMIT + 1):
        batch = api.call(
            "GET",
            "/workspaces/%s/projects" % workspace_id,
            params={"archived": "false", "page-size": PAGE_SIZE, "page": page},
        ) or []
        for project in batch:
            projects.append({
                "id": str(project.get("id") or ""),
                "name": str(project.get("name") or ""),
                "color": str(project.get("color") or ""),
                "clientName": str(project.get("clientName") or ""),
            })
        if len(batch) < PAGE_SIZE:
            break
    projects.sort(key=lambda project: project["name"].lower())
    return projects


def fetch_workspace_details(workspace_id, api):
    workspace = api.call("GET", "/workspaces/%s" % workspace_id) or {}
    settings = workspace.get("workspaceSettings")
    if not isinstance(settings, dict):
        settings = {}
    return {
        "name": str(workspace.get("name") or ""),
        "projectRequired": bool(settings.get("forceProjects")),
    }


def optional_workspace_details(workspace_id, api):
    try:
        details = fetch_workspace_details(workspace_id, api)
        details["loaded"] = True
        return details
    except ApiError:
        return {"name": "", "projectRequired": False, "loaded": False}


def time_entries(ident, api, params):
    query = {"hydrated": "true"}
    query.update(params)
    return api.call(
        "GET",
        "/workspaces/%s/user/%s/time-entries" % (ident.workspace_id, ident.user_id),
        params=query,
    ) or []


def ticket_number(description):
    text = str(description or "").strip()
    if not text.startswith("#"):
        return ""
    digits = []
    for character in text[1:]:
        if not character.isdigit():
            break
        digits.append(character)
    return "".join(digits)


def recent_ticket_entries(entries, projects_by_id):
    views = [entry_view(entry, projects_by_id) for entry in entries]
    views.sort(key=lambda entry: str(entry["start"]), reverse=True)
    recent = []
    seen = set()
    for view in views:
        description = str(view["description"] or "").strip()
        key = description.casefold()
        if not ticket_number(description) or key in seen:
            continue
        seen.add(key)
        recent.append({
            "description": description,
            "projectId": str(view["projectId"] or ""),
            "projectName": str(view["projectName"] or ""),
            "clientName": str(view["clientName"] or ""),
            "lastUsed": str(view["start"] or ""),
        })
        if len(recent) >= HISTORY_LIMIT:
            break
    return recent


def recent_task_entries(entries, projects_by_id, limit=3):
    views = [entry_view(entry, projects_by_id) for entry in entries]
    views.sort(key=lambda entry: str(entry["start"]), reverse=True)
    recent = []
    seen = set()
    for view in views:
        description = str(view["description"] or "").strip()
        key = (description.casefold(), str(view["projectId"] or ""))
        if view["running"] or not description or key in seen:
            continue
        seen.add(key)
        recent.append({
            "description": description,
            "projectId": str(view["projectId"] or ""),
            "projectName": str(view["projectName"] or ""),
            "clientName": str(view["clientName"] or ""),
            "lastUsed": str(view["start"] or ""),
        })
        if len(recent) >= limit:
            break
    return recent


def fetch_recent_history(ident, api, projects_by_id):
    entries = []
    for page in range(1, HISTORY_PAGE_LIMIT + 1):
        batch = time_entries(ident, api, {"page-size": PAGE_SIZE, "page": page})
        entries.extend(batch)
        ticket_entries = recent_ticket_entries(entries, projects_by_id)
        if len(ticket_entries) >= HISTORY_LIMIT or len(batch) < PAGE_SIZE:
            break
    return {
        "ticketEntries": recent_ticket_entries(entries, projects_by_id),
        "tasks": recent_task_entries(entries, projects_by_id),
    }


def fetch_recent_ticket_entries(ident, api, projects_by_id):
    return fetch_recent_history(ident, api, projects_by_id)["ticketEntries"]


def elapsed_whole_seconds(start, end):
    if not start or not end:
        return 0
    try:
        return max(0, int((end - start).total_seconds()))
    except (OverflowError, ValueError):
        return 0


def entry_view(entry, projects_by_id):
    interval = entry.get("timeInterval") or {}
    start = parse_stamp(interval.get("start"))
    end = parse_stamp(interval.get("end"))
    project = entry.get("project") if isinstance(entry.get("project"), dict) else {}
    project_id = str(entry.get("projectId") or project.get("id") or "")
    # `hydrated=true` normally carries the project inline; the projects list is
    # the fallback for entries where it does not.
    known = projects_by_id.get(project_id) or {}
    return {
        "id": str(entry.get("id") or ""),
        "description": str(entry.get("description") or "").strip(),
        "projectId": project_id,
        "projectName": str(project.get("name") or known.get("name") or ""),
        "projectColor": str(project.get("color") or known.get("color") or ""),
        "clientName": str(project.get("clientName") or known.get("clientName") or ""),
        "billable": bool(entry.get("billable")),
        "start": str(interval.get("start") or ""),
        "end": str(interval.get("end") or ""),
        "seconds": elapsed_whole_seconds(start, end),
        "running": start is not None and end is None,
    }


def base_payload():
    return {
        "ok": True,
        "configured": False,
        "error": "",
        "reason": "",
        "note": "",
        "userName": "",
        "workspaceId": "",
        "workspaceName": "",
        "projectRequired": False,
        "workspaceSettingsLoaded": False,
        "defaultProjectId": "",
        "weekStart": "monday",
        "running": None,
        "todaySeconds": 0,
        "weekSeconds": 0,
        "projects": [],
        "projectsLoaded": False,
        "recentEntries": [],
        "recentTasks": [],
        "historyLoaded": False,
        "fetchedAt": "",
    }


def snapshot(config, api, ident, with_projects=False):
    projects = fetch_projects(ident.workspace_id, api) if with_projects else []
    projects_by_id = {project["id"]: project for project in projects}
    workspace = (
        optional_workspace_details(ident.workspace_id, api)
        if with_projects
        else {"name": "", "projectRequired": False, "loaded": False}
    )
    recent_history = (
        fetch_recent_history(ident, api, projects_by_id)
        if with_projects
        else {"ticketEntries": [], "tasks": []}
    )

    running_entries = time_entries(ident, api, {"in-progress": "true"})
    running = entry_view(running_entries[0], projects_by_id) if running_entries else None

    first_day = str(config.get("weekStart") or "monday")
    moment = now_utc()
    # One fetch feeds both totals, so it only has to reach back to the start of
    # the calendar week.
    week_floor = week_start(moment, first_day)
    window = time_entries(ident, api, {
        "start": utc_stamp(week_floor),
        "end": utc_stamp(moment),
        "page-size": PAGE_SIZE,
    })
    entries = [entry_view(entry, projects_by_id) for entry in window]
    finished = [entry for entry in entries if not entry["running"]]

    # Totals cover closed entries only. The running entry's elapsed time is
    # added live in QML, which ticks a clock anyway and would otherwise show a
    # today total that freezes between refreshes.
    today_floor = utc_stamp(local_midnight(moment))
    week_stamp = utc_stamp(week_floor)

    payload = base_payload()
    payload.update({
        "configured": True,
        "userName": ident.user_name,
        "workspaceId": ident.workspace_id,
        "workspaceName": workspace["name"],
        "projectRequired": workspace["projectRequired"],
        "workspaceSettingsLoaded": workspace["loaded"],
        "defaultProjectId": default_project_id(config),
        "weekStart": first_day,
        "running": running,
        "todaySeconds": sum(e["seconds"] for e in finished if str(e["start"]) >= today_floor),
        "weekSeconds": sum(e["seconds"] for e in finished if str(e["start"]) >= week_stamp),
        "projects": projects,
        "projectsLoaded": bool(with_projects),
        "recentEntries": recent_history["ticketEntries"],
        "recentTasks": recent_history["tasks"],
        "historyLoaded": bool(with_projects),
        "fetchedAt": utc_stamp(moment),
    })
    return payload


def stop_running(ident, api):
    """Stop the running entry. False (not an error) when there was none."""
    try:
        api.call(
            "PATCH",
            "/workspaces/%s/user/%s/time-entries" % (ident.workspace_id, ident.user_id),
            body={"end": utc_stamp(now_utc())},
        )
    except ApiError as error:
        # Clockify answers 404 when nothing is running. That is the expected
        # answer for a redundant stop, and it must not abort a start.
        if error.code == 404:
            return False
        raise
    return True


# ---------------------------------------------------------------- commands

def open_api(config):
    return Api(api_key(config), str(config.get("apiHost") or ""))


def remember_host(config, api):
    """Persist the address that answered without restoring stale credentials."""
    host = str(api.good_host or "")
    if host and host != str(config.get("apiHost") or ""):
        update_config({"apiHost": host})


def unconfigured(config, note=""):
    payload = base_payload()
    payload["note"] = note
    payload["weekStart"] = str(config.get("weekStart") or "monday")
    payload["defaultProjectId"] = default_project_id(config)
    return payload


def cmd_status(args):
    config = load_config()
    key = api_key(config)
    if not key:
        return unconfigured(config)
    api = open_api(config)
    payload = snapshot(config, api, identity(identity_config(config), api), with_projects=args.projects)
    remember_host(config, api)
    return payload


def cmd_start(args):
    config = load_config()
    key = api_key(config)
    if not key:
        return unconfigured(config)
    api = open_api(config)
    ident = identity(identity_config(config), api)
    description = (
        sys.stdin.readline() if args.description_stdin else str(args.description or "")
    ).strip()
    # An omitted --project falls back to the remembered one; an empty --project
    # is the user deliberately logging time without a project.
    project_id = default_project_id(config) if args.project is None else str(args.project).strip()
    workspace = fetch_workspace_details(ident.workspace_id, api)
    if workspace["projectRequired"] and not project_id:
        raise ApiError(
            "This workspace requires a project before a timer can start",
            reason="PROJECT_REQUIRED",
        )

    if (
        not environment_api_key()
        and args.project is not None
        and project_id != default_project_id(config)
    ):
        config = update_config({"defaultProjectId": project_id})
    remember_host(config, api)

    stop_running(ident, api)
    body: dict[str, object] = {"start": utc_stamp(now_utc())}
    if description:
        body["description"] = description
    if project_id:
        body["projectId"] = project_id
    if args.billable:
        body["billable"] = True
    api.call("POST", "/workspaces/%s/time-entries" % ident.workspace_id, body=body)

    try:
        payload = snapshot(config, api, ident)
    except ApiError as error:
        raise ApiError(
            "Timer started, but its updated status could not be loaded: %s" % error,
            reason="TIMER_STARTED",
        ) from error
    payload["note"] = "Timer started"
    return payload


def cmd_stop(args):
    config = load_config()
    key = api_key(config)
    if not key:
        return unconfigured(config)
    api = open_api(config)
    ident = identity(identity_config(config), api)
    stopped = stop_running(ident, api)
    payload = snapshot(config, api, ident)
    if stopped:
        payload["note"] = "Timer stopped"
    else:
        payload["error"] = "No timer is running"
    remember_host(config, api)
    return payload


def cmd_set_key(args):
    key = sys.stdin.readline().strip()
    if not key:
        payload = base_payload()
        payload["ok"] = False
        payload["error"] = "No API key received"
        return payload
    if environment_api_key():
        payload = base_payload()
        payload["ok"] = False
        payload["configured"] = True
        payload["error"] = "CLOCKIFY_API_KEY is active; remove it before storing a different key"
        return payload

    # Validated before it is written, so a typo never gets stored and then
    # blamed on Clockify at the next refresh.
    config = load_config()
    api = Api(key, str(config.get("apiHost") or ""))
    ident = identity({}, api)
    removals = (
        ("defaultProjectId",)
        if str(config.get("workspaceId") or "") != ident.workspace_id
        else ()
    )
    config = update_config(
        {"apiKey": key, "workspaceId": ident.workspace_id},
        removals=removals,
    )

    payload = snapshot(config, api, ident, with_projects=True)
    payload["note"] = "Connected as %s" % (ident.user_name or "your Clockify account")
    remember_host(config, api)
    return payload


def cmd_clear_key(args):
    config = update_config(removals=("apiKey", "workspaceId", "defaultProjectId"))
    if environment_api_key():
        api = open_api(config)
        ident = identity({}, api)
        payload = snapshot(config, api, ident, with_projects=True)
        payload["note"] = "Stored API key removed; CLOCKIFY_API_KEY is still active"
        remember_host(config, api)
        return payload
    return unconfigured(config, note="Stored API key removed")


def cmd_set_config(args):
    changes = {}
    if args.project is not None and not environment_api_key():
        changes["defaultProjectId"] = str(args.project).strip()
    if args.workspace is not None:
        changes["workspaceId"] = str(args.workspace).strip()
    if args.week_start is not None:
        changes["weekStart"] = str(args.week_start).strip().lower()
    config = update_config(changes, reset_project_for_workspace=True)
    return {
        "ok": True,
        "error": "",
        "note": "Saved",
        "configured": bool(api_key(config)),
        "defaultProjectId": default_project_id(config),
        "workspaceId": str(config.get("workspaceId") or ""),
        "weekStart": str(config.get("weekStart") or "monday"),
    }


def build_parser():
    parser = argparse.ArgumentParser(description="Clockify bridge for the Omarchy shell")
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status", help="current timer and totals")
    status.add_argument("--projects", action="store_true", help="also fetch the project list")
    status.set_defaults(handler=cmd_status)

    start = commands.add_parser("start", help="stop whatever runs and start a new entry")
    start.add_argument("--description", default="")
    start.add_argument("--description-stdin", action="store_true", help=argparse.SUPPRESS)
    start.add_argument("--project", default=None, help="project id, or empty for none")
    start.add_argument("--billable", action="store_true")
    start.set_defaults(handler=cmd_start)

    stop = commands.add_parser("stop", help="stop the running entry")
    stop.set_defaults(handler=cmd_stop)

    set_key = commands.add_parser("set-key", help="read an API key from stdin and store it")
    set_key.set_defaults(handler=cmd_set_key)

    clear_key = commands.add_parser("clear-key", help="forget the stored API key")
    clear_key.set_defaults(handler=cmd_clear_key)

    set_config = commands.add_parser("set-config", help="update stored preferences")
    set_config.add_argument("--project", default=None, help="remembered project id")
    set_config.add_argument("--workspace", default=None)
    set_config.add_argument("--week-start", default=None, choices=["monday", "sunday"])
    set_config.set_defaults(handler=cmd_set_config)

    return parser


def main(argv):
    args = build_parser().parse_args(argv)
    try:
        payload = args.handler(args)
    except ApiError as error:
        payload = base_payload()
        payload["ok"] = False
        payload["error"] = str(error)
        payload["reason"] = error.reason
        payload["configured"] = bool(api_key(load_config()))
    except Exception as error:  # a traceback on stdout would break the parser
        payload = base_payload()
        payload["ok"] = False
        payload["error"] = "Clockify helper failed: %s" % (error,)
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
