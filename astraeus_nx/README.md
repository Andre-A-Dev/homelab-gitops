# astraeus_nx

Same physical machine as `astraeus_win/`, second SSD booting CachyOS (Arch-based
Linux). CachyOS is the primary environment and the development machine for
this repo; Windows 11 (`astraeus_win/`) is only booted for a few exceptions.

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

`shared/workstations/plasmoids/local.homelab.roomclimate/` is a Plasma 6 panel widget showing the
Netatmo values from Prometheus. It needs the Caddy CA above.
`shared/workstations/install.sh` installs it together with the AstraeusNX
dotfiles in `dotfiles/`; settings and updates:
`shared/workstations/plasmoids/local.homelab.roomclimate/README.md`.

## PrusaSlicer 3.0 alpha

The stable PrusaSlicer comes from pacman (`prusa-slicer`, Arch `extra`). The
3.0 alpha runs next to it as a Flatpak from `flathub-beta`; Prusa ships no
Linux binaries on GitHub, Flathub is its only Linux channel.

```fish
flatpak remote-add --if-not-exists flathub-beta https://flathub.org/beta-repo/flathub-beta.flatpakrepo
flatpak install flathub-beta com.prusa3d.PrusaSlicer
flatpak run --branch=beta com.prusa3d.PrusaSlicer
```

The alpha keeps its profiles in
`~/.var/app/com.prusa3d.PrusaSlicer/config/PrusaSlicer3-dev/`, separate from
the stable `~/.config/PrusaSlicer/`. `flatpak update` updates it;
`system-update.sh` does not touch Flatpaks.

Keep the stable build on pacman as long as the alpha is in use: both Flathub
branches share one app ID, and only the current branch gets a menu entry.

**Sign-in:** the alpha's embedded sign-in window fails with
`WebViewDialog error: wxWEBVIEW_NAV_ERR_OTHER Load request cancelled`.
Browser sign-in works, but the `prusaslicer://` callback opens the stable
build, which owns that URL scheme (`custom-prusaslicer-handler.desktop` in
`~/.config/mimeapps.list`). With the alpha running:

```fish
prusa-alpha-login
```

It points the scheme at the alpha, waits for Enter after the sign-in and
points it back. The alpha refreshes its token itself, so this is only needed
after a sign-out or an expired session.
