"""Checks for moonraker/zmod_services.py with a fake Moonraker.

Run: python test_zmod_services.py  (POSIX: the start/stop test runs real
/bin/sh init scripts; it is skipped on Windows.)
"""
import asyncio
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "moonraker"))
import zmod_services as zs  # noqa: E402

FAILS = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


class FakeConfig:
    def __init__(self, opts=None, server=None):
        self.opts = opts or {}
        self.server = server

    def get(self, key, default=None):
        return self.opts.get(key, default)

    def get_server(self):
        return self.server

    def getsection(self, name):
        return FakeConfig({}, self.server)

    def getchoice(self, key, choices, default, force_lowercase=False):
        return default


class FakeStats:
    def __init__(self):
        self.cbs = []

    def register_stat_callback(self, cb):
        self.cbs.append(cb)


class FakeServer:
    error = RuntimeError

    def __init__(self):
        self.events = []
        self.comps = {"proc_stats": FakeStats()}

    def lookup_component(self, name):
        return self.comps[name]

    def load_component(self, config, name):
        return object()

    def send_event(self, ev, data):
        self.events.append((ev, data))


class FakeBase:
    def __init__(self, config):
        self.server = config.get_server()
        self.available_services = {}

    async def reboot(self):
        return "base-reboot"

    async def shutdown(self):
        return "base-shutdown"

    def is_service_available(self, s):
        return s in self.available_services

    def get_available_services(self):
        return self.available_services


# --- pure helpers ---------------------------------------------------------
check("condition empty", zs.condition_met("", []))
check("condition present", zs.condition_met("helix = 1", ["helix = 1"]))
check("condition absent", not zs.condition_met("guppy = 1", ["guppy = 0"]))
check("negated condition", zs.condition_met("!web_screen = 0", ["x"]))
check("negated condition hit",
      not zs.condition_met("!web_screen = 0", ["web_screen = 0"]))
check("is_running substring",
      zs.is_running(("helix-screen",), ["/srv/helixscreen/bin/helix-screen "
                                        "--splash-pid=1"]))
check("is_running none", not zs.is_running(("zcam/zcam",), ["sh -c x"]))
extra = zs.parse_extra("\n  foo: S90foo, /usr/bin/foo\n# c\nbar: /x/S1bar\n")
check("parse_extra count", len(extra) == 2)
check("parse_extra script", extra[0].script == "/etc/init.d/S90foo")
check("parse_extra match", extra[0].matches == ("/usr/bin/foo",))
check("parse_extra default match", extra[1].matches == ("bar",))

# --- build_services against a fake init.d and variables.cfg ---------------
with tempfile.TemporaryDirectory() as d:
    init = os.path.join(d, "init.d")
    os.mkdir(init)
    for s in ("S65moonraker", "S70httpd", "S80helixscreen", "S80guppyscreen",
              "S99camera", "S71screen", "S90foo"):
        open(os.path.join(init, s), "w").close()
    var = os.path.join(d, "variables.cfg")
    with open(var, "w") as f:
        f.write("helix = 1\nguppy = 0\nweb_screen = 0\n")
    zs.INIT_DIR = init
    cfg = FakeConfig({"variables": var, "hide": "camera",
                      "services": f"foo: {init}/S90foo, foobin"})
    names = [s.name for s in zs.build_services(cfg)]
    check("helix chosen, guppy not", "helixscreen" in names
          and "guppyscreen" not in names)
    check("hide works", "camera" not in names)
    check("web_screen off hidden", "web_screen" not in names)
    check("missing script skipped", "orca_ff" not in names)
    check("extra added", "foo" in names)
    check("moonraker restart only", [s for s in zs.build_services(cfg)
                                     if s.name == "moonraker"][0].actions
          == ("restart",))
    zs.INIT_DIR = "/etc/init.d"

# --- provider swap and actions --------------------------------------------
server = FakeServer()
old = FakeBase(FakeConfig({}, server))
fake_machine_mod = types.ModuleType("fake_machine")
fake_machine_mod.BaseProvider = FakeBase


class Machine:
    pass


Machine.__module__ = "fake_machine"
sys.modules["fake_machine"] = fake_machine_mod
machine = Machine()
machine.sys_provider = old
server.comps["machine"] = machine


async def run_provider_tests():
    with tempfile.TemporaryDirectory() as d:
        zs.INIT_DIR = d
        marker = f"zsvc_test_{os.getpid()}"
        script = os.path.join(d, "S90dummy")
        with open(script, "w") as f:
            f.write(f"""#!/bin/sh
case "$1" in
  start) sh -c 'sleep 300; : {marker}' >/dev/null 2>&1 & ;;
  stop) pkill -f "{marker}" ;;
esac
""")
        cfg = FakeConfig({"variables": "/nonexistent",
                          "services": f"dummy: S90dummy, {marker}"}, server)
        comp = zs.load_component(cfg)
        prov = machine.sys_provider
        check("provider swapped", prov is not old and comp is not None)
        check("reboot kept", await prov.reboot() == "base-reboot")
        await prov.initialize()
        check("dummy listed stopped",
              prov.get_available_services().get("dummy", {})
              .get("sub_state") == "dead")
        check("stat callback registered",
              len(server.comps["proc_stats"].cbs) == 1)
        await prov.do_service_action("start", "dummy")
        check("start -> running",
              prov.available_services["dummy"]["sub_state"] == "running")
        check("state event sent", any(e[0] == "machine:service_state_changed"
                                      for e in server.events))
        await prov.do_service_action("restart", "dummy")
        check("restart -> running",
              prov.available_services["dummy"]["sub_state"] == "running")
        await prov.do_service_action("stop", "dummy")
        check("stop -> dead",
              prov.available_services["dummy"]["sub_state"] == "dead")
        await prov.do_service_action("restart", "klipper")
        check("unknown service ignored", True)
        prov.services["dummy"].actions = ("restart",)
        try:
            await prov.do_service_action("stop", "dummy")
            check("disallowed action refused", False)
        except RuntimeError:
            check("disallowed action refused", True)
        zs.INIT_DIR = "/etc/init.d"


if os.name == "posix":  # Windows paths break the init.d checks
    asyncio.run(run_provider_tests())
else:
    print("skip provider start/stop tests (not POSIX)")

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
