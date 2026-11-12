# ~/homelab-infra/shared/workstations/dotfiles/fish/functions/homelab-keys.fish
#
# Open and lock the offline key stick at /mnt/keys. Both sticks open under
# the same mapper name, so every RUNBOOK command works with either one.
# See RUNBOOK "Emergency SSH Key" and "Offline Key Stick".
function homelab-keys --description "Open or lock the offline key stick at /mnt/keys"
    set -l usage "Usage: homelab-keys open [main|backup]  |  homelab-keys close"

    switch "$argv[1]"
        case open
            if set -q argv[2]; and not contains -- $argv[2] main backup
                echo $usage >&2
                return 2
            end

            if test -e /dev/mapper/homelab-keys
                # Already unlocked: which stick? sysfs maps the mapper to its partition.
                set -l open none
                for dm in /sys/block/dm-*
                    test "$(cat $dm/dm/name)" = homelab-keys; or continue
                    set -l part /dev/(path basename $dm/slaves/*)
                    for s in main backup
                        test "$(path resolve /dev/disk/by-label/homelab-keys-$s)" = $part; and set open $s
                    end
                end
                if set -q argv[2]; and test $open != $argv[2]
                    echo "homelab-keys-$open is open, not homelab-keys-$argv[2]. Lock it first: homelab-keys close" >&2
                    return 1
                end
                echo "homelab-keys-$open already unlocked"
            else
                # Stick by LUKS label: the named one, or the only one plugged in
                set -l sticks
                if set -q argv[2]
                    set sticks $argv[2]
                else
                    for s in main backup
                        test -e /dev/disk/by-label/homelab-keys-$s; and set -a sticks $s
                    end
                end
                if test (count $sticks) -ne 1
                    echo "Key sticks found: "(string join ', ' -- $sticks; or echo none)". Name one: homelab-keys open main|backup" >&2
                    return 1
                end

                set -l dev /dev/disk/by-label/homelab-keys-$sticks[1]
                if not test -e $dev
                    echo "$dev not found - stick plugged in?" >&2
                    return 1
                end
                echo "Opening homelab-keys-$sticks[1] ($(path resolve $dev))"
                sudo cryptsetup open $dev homelab-keys; or return 1
            end

            if not mountpoint -q /mnt/keys
                sudo mkdir -p /mnt/keys
                if not sudo mount /dev/mapper/homelab-keys /mnt/keys
                    sudo cryptsetup close homelab-keys
                    return 1
                end
            end
            findmnt /mnt/keys

        case close
            if mountpoint -q /mnt/keys
                if not sudo umount /mnt/keys
                    echo "Still in use (cd out, close the editor):" >&2
                    sudo fuser -vm /mnt/keys
                    return 1
                end
            end
            if test -e /dev/mapper/homelab-keys
                sudo cryptsetup close homelab-keys; or return 1
            end
            echo "Locked. The stick can be unplugged."

        case '*'
            echo $usage >&2
            return 2
    end
end
