#!/usr/bin/env bash
# The interactive login walkthrough for invective-dev, baked into the image at
# /home/dev/login.sh. Run from a terminal on zero with:
#
#     cd ~/infra/dev && make login C=invective-dev
#
# Every step reads the current state and prompts only when something is not set up, so
# running it again is safe and near-instant. Nothing here is fatal: a step that cannot finish
# says why and the next one runs. Where each thing lives:
#
#   GitHub       -> /run/github, the hub's token   (nothing to do in here; checked)
#   Claude Code  -> the login volume infra's compose names (shared by default)
#   /workspace   -> this container's volume, cloned from DEV_REPO (here, if start-up could not)
#
# There is no ssh identity step: invective-dev needs none.
set -u

bold() { printf '\n\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
info() { printf '    %s\n' "$1"; }
ask()  { # ask "question" -> exit 0 on yes (an empty answer is yes)
  local reply
  read -r -p "  $1 [Y/n] " reply || return 1
  [ -z "$reply" ] || [ "$reply" = y ] || [ "$reply" = Y ]
}

REPO="${DEV_REPO:-gsfernandes81/invective}"

# ── 1/2  GitHub: the hub's token ─────────────────────────────────────────────
# Nothing to log into: the hub mints this container an hourly token for this repo alone, and
# git (its credential helper) and gh (its wrapper) both use it. So this only reports, and not
# through `gh auth status`, which fails for an installation token (it has no user) while
# every repo command works.
#
# Asked of the API, not with `git ls-remote`: invective is public, so GitHub answers a read
# of it without asking for any credential, and a missing or rejected token would pass. The
# token's own list of repositories is refused for a bad token and names the repo for a good
# one; gh reads the token through the base's wrapper.
bold "1/2  GitHub (the hub's token)"
if [ -s /run/github/token ]; then
  if covered=$(gh api installation/repositories --paginate --jq '.repositories[].full_name' 2>&1) \
      && printf '%s\n' "$covered" | grep -qixF "$REPO"; then
    ok "the hub's token covers ${REPO}: git and gh need nothing else."
  else
    warn "a token is here but GitHub does not take it for ${REPO}:"
    info "$(printf '%s' "$covered" | head -1)"
    info "Is the App installed on that repo? On zero: cd ~/infra/dev && make status C=hub"
  fi
else
  warn "no token from the hub yet."
  info "The hub writes one at its start and every 30 min; on zero, make status C=hub"
  info "says why it has not."
fi

# ── 2/2  Claude Code ─────────────────────────────────────────────────────────
# Logging in here is what makes `claude` work in the container at all. The login is shared
# across the dev containers by default (infra's CLAUDE_UNDOCUMENTED_SHARED_LOGINS switch), so
# a login made in any of them is already this one's. An OAuth login belongs to the device
# that made it: never copy a credentials file in from elsewhere.
bold "2/2  Claude Code"
if claude auth status 2>/dev/null | grep -q '"loggedIn": *true'; then
  ok "logged in: \`claude\` will start without a login flow."
else
  warn "Not logged in."
  info "No browser in here: it prints a URL, and you paste back the code."
  ask "Run 'claude auth login' now?" \
    && claude auth login --claudeai --email "${GIT_USER_EMAIL:-gavinfernandes2012@pm.me}"
fi

# ── the workspace ────────────────────────────────────────────────────────────
# /workspace is this container's own volume, cloned at first start from DEV_REPO with the
# hub's token. A start before the token arrived leaves it without a checkout; this is where
# it gets one, and child-init.sh then adds the project to the venv as a start would have.
bold "the workspace (/workspace)"
if [ -d /workspace/.git ]; then
  ok "$(git -C /workspace remote get-url origin 2>/dev/null) at $(git -C /workspace log --oneline -1 2>/dev/null)"
elif [ -n "$(ls -A /workspace 2>/dev/null)" ]; then
  warn "/workspace holds files but no checkout; not cloning ${REPO} over them."
  info "If they are only the image's pyproject.toml and uv.lock, start-up clones and adopts"
  info "them: restart the container (make restart C=invective-dev) once the token is here."
elif ask "Clone ${REPO} into /workspace now?"; then
  if git clone "https://github.com/${REPO}.git" /workspace; then
    bash /home/dev/child-init.sh || warn "child-init.sh failed; its output is above."
  else
    warn "the clone failed: step 1 above says whether the token is the reason."
  fi
fi

bold "Done."
cat <<'EOF'

  invective-dev is reached through the hub. From the phone or a PC:

      ssh dev                 every dev container; Enter on invective-dev for its
                              sessions, s for a shell in it

  Every session the menu opens is a zmx session: Ctrl-\ detaches and the menu
  comes back, and a dropped connection costs nothing: ssh in again and press Enter.

  In /workspace, the suite is `pytest` (the venv is on PATH; `uv run pytest` works too).

  From a terminal on zero, in ~/infra/dev:
      make up S=invective-dev     start it
      make shell C=invective-dev  a shell in it, without the hub
      make status C=invective-dev what it is running, and which logins it has
      make login C=invective-dev  these steps again
EOF
