# zmod_services: Service Control for Zmod's Moonraker.
#
# Zmod runs its services from init.d scripts in its chroot (BusyBox init,
# no systemd). Its Moonraker is set to provider systemd_cli, and Zmod's
# /bin/systemctl is a stub that only handles reboot and poweroff, so
# Moonraker finds no services and Mainsail/Fluidd hide Service Control.
#
# This component swaps Moonraker's machine provider for one that knows
# Zmod's init.d services: it lists them, reports running/stopped from
# /proc, and runs the scripts for start/stop/restart. Reboot and shutdown
# still go through Zmod's stub (BaseProvider), as before.
#
# install.sh links this file into Moonraker's components; the
# [zmod_services] section in zmod_services.moonraker.conf loads it.
# Moonraker builds every component before it runs any component_init, so
# the swap is done before machine asks its provider for the services.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Tuple

INIT_DIR = "/etc/init.d"
VARIABLES = "/opt/config/mod_data/variables.cfg"
STOP_WAIT = 20.0
ACTIONS = ("start", "stop", "restart")

# name, init script, processes that mean "running" (any of them, matched
# against /proc/<pid>/cmdline), condition in variables.cfg ("" = always,
# "!x = 0" = unless that line is there), allowed actions.
# Scripts whose start command is not "start" (S99camera's start does
# nothing; its own restart is stop + up).
START_ARG = {"camera": "up"}
# Klipper is not here: on Zmod it runs on the host, outside the chroot,
# under the stock firmware; Mainsail's Klipper Control restarts it.
BUILTIN: List[Tuple[str, str, Tuple[str, ...], str, Tuple[str, ...]]] = [
    ("moonraker", "S65moonraker", ("moonraker.py",), "", ("restart",)),
    # Stopping the web server would cut off the page that would start it
    ("nginx", "S70httpd",
     ("nginx: master process /usr/data/zmod", "zmod_httpd"), "",
     ("restart",)),
    ("helixscreen", "S80helixscreen", ("helix-screen",), "helix = 1",
     ACTIONS),
    ("guppyscreen", "S80guppyscreen", ("guppyscreen/guppyscreen",),
     "guppy = 1", ACTIONS),
    ("camera", "S99camera", ("zcam/zcam", "mjpg_streamer", "ustreamer"),
     "", ACTIONS),
    ("web_screen", "S71screen", ("/root/screen ",), "!web_screen = 0",
     ACTIONS),
    ("orca_ff", "S72orca_ff", ("orca_ff",), "", ACTIONS),
]


def script_env() -> Dict[str, str]:
    # The scripts run inside the chroot: no host library paths or preloads
    # (the host's tslib LD_PRELOAD makes chroot programs complain/crash)
    env = {k: v for k, v in os.environ.items()
           if k not in ("LD_PRELOAD", "LD_LIBRARY_PATH")}
    env.setdefault("PATH", "/bin:/sbin:/usr/bin:/usr/sbin")
    return env


def read_variables(path: str = VARIABLES) -> List[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return [line.strip() for line in f]
    except OSError:
        return []


def condition_met(cond: str, lines: List[str]) -> bool:
    if not cond:
        return True
    if cond.startswith("!"):
        return cond[1:] not in lines
    return cond in lines


def process_cmdlines(proc: str = "/proc") -> List[str]:
    # Moonraker's own process counts too: it is the moonraker service
    out: List[str] = []
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return out
    for pid in pids:
        try:
            with open(f"{proc}/{pid}/cmdline", "rb") as f:
                raw = f.read()
        except OSError:
            continue
        if raw:
            out.append(raw.replace(b"\0", b" ").decode(errors="replace"))
    return out


def is_running(matches: Tuple[str, ...], cmdlines: List[str]) -> bool:
    return any(m in c for c in cmdlines for m in matches)


class Service:
    def __init__(self, name: str, script: str, matches: Tuple[str, ...],
                 actions: Tuple[str, ...], start_arg: str = "start") -> None:
        self.name = name
        self.start_arg = start_arg
        self.script = script
        self.matches = matches
        self.actions = actions
        self.lock = asyncio.Lock()


def parse_extra(text: str) -> List[Service]:
    """'name: S90foo, foo-bin, other-bin' per line."""
    out: List[Service] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        name, rest = line.split(":", 1)
        parts = [p.strip() for p in rest.split(",") if p.strip()]
        if not parts:
            continue
        script = parts[0]
        if not script.startswith("/"):
            script = f"{INIT_DIR}/{script}"
        matches = tuple(parts[1:]) or (
            re.sub(r"^[SK]\d+", "", os.path.basename(script)),)
        out.append(Service(name.strip(), script, matches, ACTIONS))
    return out


def make_provider_class(base: type) -> type:
    class ZmodInitdProvider(base):  # type: ignore[misc, valid-type]
        def __init__(self, config: Any, services: List[Service]) -> None:
            super().__init__(config)
            self.services = {s.name: s for s in services}

        async def initialize(self) -> None:
            cmdlines = process_cmdlines()
            for svc in self.services.values():
                self.available_services[svc.name] = self._state(
                    is_running(svc.matches, cmdlines))
            names = ", ".join(self.available_services) or "none"
            logging.info(f"zmod_services: services: {names}")
            if self.available_services:
                pstats = self.server.lookup_component("proc_stats")
                pstats.register_stat_callback(self._update_service_status)

        @staticmethod
        def _state(running: bool) -> Dict[str, str]:
            if running:
                return {"active_state": "active", "sub_state": "running"}
            return {"active_state": "inactive", "sub_state": "dead"}

        async def _update_service_status(
            self, sequence: int, notify: bool = True
        ) -> None:
            if sequence % 2:
                return
            self._refresh(notify)

        def _refresh(self, notify: bool = True) -> None:
            cmdlines = process_cmdlines()
            for svc in self.services.values():
                new = self._state(is_running(svc.matches, cmdlines))
                if self.available_services.get(svc.name) != new:
                    self.available_services[svc.name] = new
                    if notify:
                        self.server.send_event(
                            "machine:service_state_changed", {svc.name: new})

        async def _run(self, script: str, action: str,
                       timeout: float = 60.) -> None:
            proc = await asyncio.create_subprocess_exec(
                "/bin/sh", script, action,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True, env=script_env())
            try:
                await asyncio.wait_for(proc.wait(), timeout)
            except asyncio.TimeoutError:
                logging.info(f"zmod_services: {script} {action} still "
                             f"running after {timeout:.0f} s")

        async def _wait_stopped(self, svc: Service) -> bool:
            loop = asyncio.get_running_loop()
            end = loop.time() + STOP_WAIT
            while loop.time() < end:
                if not is_running(svc.matches, process_cmdlines()):
                    return True
                await asyncio.sleep(.5)
            return False

        async def do_service_action(self, action: str,
                                    service_name: str) -> None:
            svc = self.services.get(service_name)
            if svc is None:
                # Zmod's systemctl stub ignored these before (update
                # manager's managed_services, e.g. klipper): stay quiet
                logging.info(f"zmod_services: '{action} {service_name}'"
                             " ignored, not a Zmod service")
                return
            if action not in svc.actions:
                raise self.server.error(
                    f"Service action '{action}' not available for "
                    f"{service_name}")
            logging.info(f"zmod_services: {action} {service_name}")
            if svc.name == "moonraker":
                # The script stops this process: run it on its own, after
                # Moonraker has answered the request. Not the script's
                # restart: it starts again after 2 s, Moonraker takes
                # longer to exit, and start-stop-daemon then skips the
                # start because the old process is still there.
                pid = os.getpid()
                # subprocess, not asyncio: asyncio kills the children it
                # still tracks when Moonraker's event loop closes
                subprocess.Popen(
                    ["/bin/sh", "-c",
                     f"sleep 1; /bin/sh {svc.script} stop; i=0; "
                     f"while kill -0 {pid} 2>/dev/null && [ $i -lt 60 ]; "
                     "do sleep 1; i=$((i+1)); done; "
                     f"kill -9 {pid} 2>/dev/null; sleep 1; "
                     f"exec /bin/sh {svc.script} start"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True,
                    close_fds=True, env=script_env())
                return
            async with svc.lock:
                if action in ("stop", "restart"):
                    await self._run(svc.script, "stop")
                    if not await self._wait_stopped(svc):
                        logging.info(f"zmod_services: {svc.name} still "
                                     f"running {STOP_WAIT:.0f} s after stop")
                if action in ("start", "restart"):
                    # Init scripts that start their daemon in the
                    # foreground are left running after the timeout
                    await self._run(svc.script, svc.start_arg, timeout=30.)
                await asyncio.sleep(1.)
                self._refresh()

    return ZmodInitdProvider


def build_services(config: Any) -> List[Service]:
    hide = set(config.get("hide", "").replace(",", " ").split())
    lines = read_variables(config.get("variables", VARIABLES))
    services: List[Service] = []
    for name, script, matches, cond, actions in BUILTIN:
        path = f"{INIT_DIR}/{script}"
        if name in hide or not os.path.isfile(path):
            continue
        if not condition_met(cond, lines):
            continue
        services.append(Service(name, path, matches, actions,
                                START_ARG.get(name, "start")))
    for svc in parse_extra(config.get("services", "")):
        if svc.name in hide or not os.path.isfile(svc.script):
            continue
        services = [s for s in services if s.name != svc.name] + [svc]
    return services


class ZmodServices:
    def __init__(self, config: Any) -> None:
        self.server = config.get_server()
        machine = self.server.lookup_component("machine")
        old = machine.sys_provider
        services = build_services(config)
        # machine.py's own BaseProvider, however Moonraker imported it
        base = sys.modules[type(machine).__module__].BaseProvider
        cls = make_provider_class(base)
        mcfg = config.getsection("machine")
        provider = cls(mcfg, services)
        # Keep the old provider's reboot/shutdown (Zmod's systemctl stub)
        provider.reboot = old.reboot
        provider.shutdown = old.shutdown
        machine.sys_provider = provider
        logging.info("zmod_services: init.d service provider in place of "
                     f"{type(old).__name__}")


def load_component(config: Any) -> ZmodServices:
    return ZmodServices(config)
