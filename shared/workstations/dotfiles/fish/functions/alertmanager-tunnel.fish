# ~/homelab-infra/shared/workstations/dotfiles/fish/functions/alertmanager-tunnel.fish
#
# alertmanager.home is read-only (Caddy answers writes with 403), so silences
# go through an SSH tunnel to Alertmanager's loopback port on Mnemosyne.
# See RUNBOOK "Silence an alert".
function alertmanager-tunnel --description "Alertmanager UI with write access (silences) via SSH tunnel"
    # LAN alias by default; away from home: alertmanager-tunnel mnemosyne-ts
    set -l host mnemosyne
    if set -q argv[1]
        set host $argv[1]
    end
    # Not 9093: keeps a local Alertmanager or anything else on that port out of the way
    set -l url http://localhost:19093

    echo "Tunnel to Alertmanager on $host -> $url"
    echo "Close with Ctrl+C. Never silence Watchdog."

    # Open the browser once the tunnel answers (SSH login may wait for the
    # agent confirmation), give up after 60 s. If ssh fails, this loop just
    # runs out; disown keeps fish from reporting the background job.
    fish -c "for i in (seq 60)
        curl -s -o /dev/null $url/-/healthy; and xdg-open $url; and exit
        sleep 1
    end" &
    disown

    # ExitOnForwardFailure: fail loudly if 19093 is taken instead of
    # running a tunnel that forwards nothing.
    ssh -N -o ExitOnForwardFailure=yes -L 19093:127.0.0.1:9093 $host
end
