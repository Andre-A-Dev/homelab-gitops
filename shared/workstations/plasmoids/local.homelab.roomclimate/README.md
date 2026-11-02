# Room Climate plasmoid

Plasma 6 panel widget for the Netatmo values in Prometheus: temperature and
CO2 of one selectable module in the panel, all modules with 24 h trends in the
popup.

It only reads the `netatmo_sensor_*` series of the netatmo-exporter on
Mnemosyne. It has no Netatmo token of its own: Netatmo rotates refresh tokens,
so a second client on the same app would invalidate the exporter's token.

## Requirements

- Plasma 6
- `https://prometheus.home` reachable and trusted. Check:

  ```fish
  curl -s -o /dev/null -w '%{http_code}\n' https://prometheus.home/-/ready
  ```

  Expected: `200`. On a certificate error, import the Caddy CA into the
  system trust store (`astraeus_nx/README.md`, "Caddy CA certificate").
  Caddy serves the site to the LAN and the Tailscale range only; away from
  home the widget needs Tailscale and a resolvable `prometheus.home`.

## Install

The widget is installed as a symlink into the repo checkout, so a `git pull`
updates it.

`shared/workstations/install.sh` creates the link on every workstation it
knows (see `shared/workstations/README.md`). Check:

```fish
kpackagetool6 -t Plasma/Applet -l | grep roomclimate
```

It must print `local.homelab.roomclimate`.

Optional, test in a window without touching the panel:

```fish
plasmawindowed local.homelab.roomclimate
```

## Add to the panel

Right-click the panel, "Add Widgets…", search for
"Room Climate" and drag it in front of the clock. If it is missing from the
list, restart the shell:

```fish
systemctl --user restart plasma-plasmashell
```

## Settings

Right-click the widget, "Configure Room Climate…". Plasma stores the settings
per machine.

| Setting            | Default                          | Note                                                    |
|--------------------|----------------------------------|---------------------------------------------------------|
| Prometheus URL     | `https://prometheus.home`        |                                                         |
| Shown in the panel | Automatic (base station)         | AstraeusNX: `Home / Office`; Proteus: the room it is used in most |
| Refresh every      | 5 minutes                        | Netatmo itself updates about every 10 minutes           |
| CO₂ warning from   | 1000 ppm                         | orange                                                  |
| CO₂ critical from  | 2000 ppm                         | red                                                     |
| Grafana dashboard  | `https://grafana.home/d/netatmo` | empty hides the button                                  |

The panel values are dimmed when Prometheus is unreachable or the last Netatmo
reading is older than 30 minutes.

## Update

After a `git pull` that changes the QML:

```fish
systemctl --user restart plasma-plasmashell
```

## Uninstall

Remove the widget from the panel, then the link:

```fish
rm ~/.local/share/plasma/plasmoids/local.homelab.roomclimate
```
