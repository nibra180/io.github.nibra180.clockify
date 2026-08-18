#!/usr/bin/env python3
"""Clockify bridge for the io.github.matyssxdxd.clockify Omarchy plugin.

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
import http.client
import json
import os
import socket
import ssl
import sys
import urllib.parse
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


class ApiError(Exception):
    """A Clockify call that failed in a way worth showing the user."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


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


def save_config(config):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    # 0600 from creation, not after: the key it holds can read and rewrite every
    # time entry in the workspace, so it must never exist as a readable file.
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def api_key(config):
    return str(config.get("apiKey") or os.environ.get("CLOCKIFY_API_KEY") or "").strip()


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
        raw.settimeout(TIMEOUT)
        raw.connect(sockaddr)
        context = ssl.create_default_context()
        secure = context.wrap_socket(raw, server_hostname=HOST)
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
        expiry = monotonic() + DEADLINE
        while candidates and monotonic() < expiry:
            candidate = candidates.pop(0)
            reused = self.connection is not None and self.address == candidate
            try:
                if not reused:
                    self._close()
                    self.connection = self._open(*candidate)
                    self.address = candidate
                self.connection.request(method, target, payload, headers)
                response = self.connection.getresponse()
                raw = response.read().decode("utf-8", "replace")
            except (OSError, http.client.HTTPException) as error:
                self._close()
                failure = error
                # A reused connection that broke is usually a keep-alive the
                # server closed, not a bad route: dial this same address once
                # more before writing it off. A timeout is the opposite -- the
                # route is black-holing, so move on immediately.
                if reused and not is_timeout(error):
                    candidates.insert(0, candidate)
                continue
            self.good_host = candidate[1][0]
            return self._decode(response.status, raw)

        raise ApiError("Clockify did not answer (%s)" % (describe(failure),))

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


def fetch_workspace_name(workspace_id, api):
    # Cosmetic only -- a failure here must not cost the user their timer view.
    try:
        for workspace in api.call("GET", "/workspaces") or []:
            if str(workspace.get("id") or "") == workspace_id:
                return str(workspace.get("name") or "")
    except ApiError:
        return ""
    return ""


def time_entries(ident, api, params):
    query = {"hydrated": "true"}
    query.update(params)
    return api.call(
        "GET",
        "/workspaces/%s/user/%s/time-entries" % (ident.workspace_id, ident.user_id),
        params=query,
    ) or []


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
        "seconds": max(0, int((end - start).total_seconds())) if start and end else 0,
        "running": start is not None and end is None,
    }


def base_payload():
    return {
        "ok": True,
        "configured": False,
        "error": "",
        "note": "",
        "userName": "",
        "workspaceId": "",
        "workspaceName": "",
        "defaultProjectId": "",
        "weekStart": "monday",
        "running": None,
        "todaySeconds": 0,
        "weekSeconds": 0,
        "projects": [],
        "projectsLoaded": False,
        "fetchedAt": "",
    }


def snapshot(config, api, ident, with_projects=False):
    projects = fetch_projects(ident.workspace_id, api) if with_projects else []
    projects_by_id = {project["id"]: project for project in projects}

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
        "workspaceName": fetch_workspace_name(ident.workspace_id, api) if with_projects else "",
        "defaultProjectId": str(config.get("defaultProjectId") or ""),
        "weekStart": first_day,
        "running": running,
        "todaySeconds": sum(e["seconds"] for e in finished if str(e["start"]) >= today_floor),
        "weekSeconds": sum(e["seconds"] for e in finished if str(e["start"]) >= week_stamp),
        "projects": projects,
        "projectsLoaded": bool(with_projects),
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
    """Persist the address that answered, for the next run to start with."""
    host = str(api.good_host or "")
    if host and host != str(config.get("apiHost") or ""):
        config["apiHost"] = host
        save_config(config)


def unconfigured(config, note=""):
    payload = base_payload()
    payload["note"] = note
    payload["weekStart"] = str(config.get("weekStart") or "monday")
    payload["defaultProjectId"] = str(config.get("defaultProjectId") or "")
    return payload


def cmd_status(args):
    config = load_config()
    key = api_key(config)
    if not key:
        return unconfigured(config)
    api = open_api(config)
    payload = snapshot(config, api, identity(config, api), with_projects=args.projects)
    remember_host(config, api)
    return payload


def cmd_start(args):
    config = load_config()
    key = api_key(config)
    if not key:
        return unconfigured(config)
    api = open_api(config)
    ident = identity(config, api)
    stop_running(ident, api)

    body = {"start": utc_stamp(now_utc())}
    description = str(args.description or "").strip()
    if description:
        body["description"] = description
    # An omitted --project falls back to the remembered one; an empty --project
    # is the user deliberately logging time without a project.
    project_id = str(config.get("defaultProjectId") or "") if args.project is None else str(args.project).strip()
    if project_id:
        body["projectId"] = project_id
    if args.billable:
        body["billable"] = True
    api.call("POST", "/workspaces/%s/time-entries" % ident.workspace_id, body=body)

    if args.project is not None and project_id != str(config.get("defaultProjectId") or ""):
        config["defaultProjectId"] = project_id
        save_config(config)

    payload = snapshot(config, api, ident)
    payload["note"] = "Timer started"
    remember_host(config, api)
    return payload


def cmd_stop(args):
    config = load_config()
    key = api_key(config)
    if not key:
        return unconfigured(config)
    api = open_api(config)
    ident = identity(config, api)
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

    # Validated before it is written, so a typo never gets stored and then
    # blamed on Clockify at the next refresh.
    config = load_config()
    api = Api(key, str(config.get("apiHost") or ""))
    ident = identity({}, api)
    config["apiKey"] = key
    config["workspaceId"] = ident.workspace_id
    save_config(config)

    payload = snapshot(config, api, ident, with_projects=True)
    payload["note"] = "Connected as %s" % (ident.user_name or "your Clockify account")
    remember_host(config, api)
    return payload


def cmd_clear_key(args):
    config = load_config()
    config.pop("apiKey", None)
    config.pop("workspaceId", None)
    save_config(config)
    return unconfigured(config, note="API key removed")


def cmd_set_config(args):
    config = load_config()
    if args.project is not None:
        config["defaultProjectId"] = str(args.project).strip()
    if args.workspace is not None:
        config["workspaceId"] = str(args.workspace).strip()
    if args.week_start is not None:
        config["weekStart"] = str(args.week_start).strip().lower()
    save_config(config)
    return {
        "ok": True,
        "error": "",
        "note": "Saved",
        "configured": bool(api_key(config)),
        "defaultProjectId": str(config.get("defaultProjectId") or ""),
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
