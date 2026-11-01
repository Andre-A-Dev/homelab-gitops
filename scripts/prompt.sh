# shellcheck shell=bash
# prompt.sh — host-aware bash prompt for all homelab servers
# Sourced, not executed — hence a shell directive instead of a shebang.
# Deployed to ~/.prompt.sh on each server and sourced from ~/.bashrc.
# Same file everywhere; color and tag are picked by hostname.
#
# Color rules:
#   - Red is reserved for errors (non-zero exit code). No host uses red.
#   - Hosts with the same role share a color family (boreas/zephyros = DNS).
#   - Every host gets a [role·site] tag, so the prompt is self-explanatory
#     even when colors look similar.

# Only for interactive shells
[[ $- != *i* ]] && return

# Hostnames are capitalized on some hosts (e.g. "Zephyros") -> normalize
case "${HOSTNAME,,}" in
  mnemosyne)  _host_color='1;38;5;141'; _host_tag='hub·home'  ;;  # purple
  boreas)     _host_color='1;38;5;33';  _host_tag='dns·home'  ;;  # blue
  zephyros)   _host_color='1;38;5;80';  _host_tag='dns·away'  ;;  # turquoise (DNS family)
  hephaestus) _host_color='1;38;5;214'; _host_tag='heat·home' ;;  # amber
  daidalos)   _host_color='1;38;5;112'; _host_tag='k8s·home'  ;;  # green
  *)          _host_color='1;38;5;250'; _host_tag='unknown'   ;;  # grey = not in list
esac

# Root: invert the host color instead of a separate (red) marker
if [[ $EUID -eq 0 ]]; then
  _host_color="7;${_host_color}"
fi

# Show exit code of the last command if non-zero (the only red in the prompt).
# \001/\002 mark non-printing bytes so line wrapping stays correct.
__prompt_status() {
  local s=$? sig
  # Newer kernel installed than running: flag set by update-notifier or
  # system-update; /run is cleared on boot, so the marker goes away by itself.
  [[ -f /run/reboot-required ]] && printf '\001\e[38;5;220m\002⟳reboot\001\e[0m\002 '
  (( s == 0 )) && return
  # >128 = killed by signal 128+n -> show its name (130 -> INT, 137 -> KILL).
  # stderr dropped on purpose: unknown signal numbers keep the plain code.
  if (( s > 128 )) && sig=$(kill -l "$((s - 128))" 2>/dev/null); then
    s=$sig
  fi
  printf '\001\e[38;5;196m\002✗%s\001\e[0m\002 ' "$s"
}

# Git marker inside a checkout: branch, * unstaged / + staged changes,
# upstream state (= in sync, < behind, > ahead, <> diverged).
# Server checkouts are deploy sources: local edits or commits break the
# fast-forward deploy and the cron pull, so make them visible.
_git_part=''
if [[ -r /usr/lib/git-core/git-sh-prompt ]]; then
  # shellcheck source=/dev/null
  . /usr/lib/git-core/git-sh-prompt
  GIT_PS1_SHOWDIRTYSTATE=1
  GIT_PS1_SHOWUPSTREAM=auto
  # shellcheck disable=SC2016  # expanded at prompt time, not here
  _git_part='$(__git_ps1 " (%s)")'
fi

# \e]0;...\a sets the terminal tab/window title -> hostname visible in tabs
PS1="\[\e]0;\u@\h [${_host_tag}]: \w\a\]"
PS1+='$(__prompt_status)'
PS1+="\[\e[${_host_color}m\]\u@\h [${_host_tag}]\[\e[0m\]"
PS1+=":\w${_git_part}\\$ "   # path in default terminal color to avoid clashing with host colors

unset _host_color _host_tag _git_part
