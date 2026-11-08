source /usr/share/cachyos-fish-config/cachyos-config.fish

# overwrite greeting
# potentially disabling fastfetch
#function fish_greeting
#    # smth smth
#end
alias lumux="source ~/.venv/lumux/bin/activate.fish && python -m lumux"

# Public mirror clone as the default target; expands on space, so the full
# command is visible before it runs (see shared/workstations/README.md)
abbr -a sync-public 'sync-public ~/Projects/Github/homelab-gitops'

# Added by LM Studio CLI (lms)
set -gx PATH $PATH /home/<user>/.lmstudio/bin
# End of LM Studio CLI section

