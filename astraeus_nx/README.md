# astraeus_nx

Same physical machine as `astraeus/`, second SSD booting CachyOS (Arch-based
Linux). CachyOS is the primary environment and the development machine for
this repo; Windows 11 (`astraeus/`) is only booted for a few exceptions.

## Caddy CA certificate

CachyOS uses the system trust store. Import the Caddy internal CA root once to
make `.home` domains trusted in browsers:

```bash
# Export the cert from the running Caddy container on Mnemosyne
ssh mnemosyne "docker exec caddy cat /data/caddy/pki/authorities/local/root.crt" \
  > ~/caddy-local-ca.crt

# Add to system trust store
sudo trust anchor --store ~/caddy-local-ca.crt
sudo update-ca-trust
```

Electron apps (VSCodium, Bitwarden desktop) ignore the system CA and need a
separate NSS database entry:

```bash
certutil -d sql:$HOME/.pki/nssdb \
  -A -t "CT,," -n "Caddy Local CA" -i ~/caddy-local-ca.crt
```

Run this for each user profile that runs Electron apps.

## Room Climate plasmoid

`plasmoids/local.homelab.roomclimate/` is a Plasma 6 panel widget showing the
Netatmo values (temperature, CO2, humidity, 24 h trend) from Prometheus. It
only reads the `netatmo_sensor_*` series of the netatmo-exporter; it has no
Netatmo token of its own. Needs the Caddy CA above, since it queries
`https://prometheus.home` through Qt's system trust store.

- Install: `dotfiles/install.sh` symlinks it to
  `~/.local/share/plasma/plasmoids/`. Then right-click the panel, "Add
  Widgets…", "Room Climate".
- Settings (stored per machine by Plasma): module shown in the panel (on
  AstraeusNX: `Home / Office`), CO2 thresholds, refresh interval, Grafana link.
  Without a choice it shows the first base station.
- After pulling changes to the QML: `systemctl --user restart plasma-plasmashell`.
- Test without touching the panel:
  `plasmawindowed local.homelab.roomclimate` opens it in a window.
