# zmod_services

Adds **Service Control** to the power menu in Mainsail and Fluidd on Zmod printers (Flashforge AD5X, AD5M, Creator 5 Pro). It works like it does on a systemd printer: each service shows whether it is running and has start, stop and restart buttons.

## Why Zmod doesn't have it

Mainsail and Fluidd ask Moonraker for the services. Moonraker gets them from the init system (systemd or supervisord). Zmod uses BusyBox `init.d` scripts in its chroot. Its Moonraker is set to `systemd_cli`, but Zmod's `/bin/systemctl` is a stub that only handles reboot and poweroff. So the service list is empty and the section is hidden.

## How it works

The plugin is a runtime patch. It does not change any Zmod files. `install.sh` links `moonraker/zmod_services.py` into Zmod's Moonraker as a component. At startup that component replaces Moonraker's machine provider with one that knows Zmod's `init.d` services:

- **Running or stopped:** read from `/proc` every few seconds. Mainsail and Fluidd update live.
- **Start, stop and restart:** run the service's script in the chroot's `/etc/init.d`. A restart is a stop, then a wait until the process is gone (up to 20 s), then a start. This avoids the race where HelixScreen is still running when its start script checks.
- **Reboot and shutdown:** still go through Zmod's own stub, as before.

| Service | Script | Actions |
|---|---|---|
| moonraker | S65moonraker | restart |
| nginx (web interface) | S70httpd | restart |
| helixscreen *or* guppyscreen | S80helixscreen / S80guppyscreen | start, stop, restart (only the one Zmod is set to) |
| camera | S99camera | start, stop, restart |
| web_screen | S71screen | start, stop, restart (unless `web_screen = 0`) |
| orca_ff | S72orca_ff | start, stop, restart |

Klipper is not in the list. On Zmod it runs on the host under the stock firmware, outside the chroot. Use **Klipper Control → Restart / Firmware Restart** in the same menu.

## Install

Add this to `mod_data/user.moonraker.conf`:

```ini
[update_manager zmod_services]
type: git_repo
channel: dev
path: /root/printer_data/config/mod_data/plugins/zmod_services
origin: https://github.com/DIEHARDave/zmod_services.git
is_system_service: False
primary_branch: main
```

Then run `ENABLE_PLUGIN name=zmod_services` in the console. The printer reboots, and Service Control appears in the power menu. `DISABLE_PLUGIN name=zmod_services` removes it again.

After a Zmod update replaces Moonraker, run `install.sh` again (or `ENABLE_PLUGIN` again) to restore the component link.

## Options

All options go in `zmod_services.moonraker.conf`. Changes take effect after a Moonraker restart.

```ini
[zmod_services]
# Built-in services to leave out
hide: orca_ff web_screen
# More services: name: init script, process(es) that show it is running
services:
  myservice: S90myservice, /usr/bin/myservice
```

## Tests

`python3 test_zmod_services.py` (Linux or WSL) runs the service list, conditions and start/stop/restart against fake `init.d` scripts.
