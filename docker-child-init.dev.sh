#!/usr/bin/env bash
# invective-dev's own start-up, baked into the image at /home/dev/child-init.sh and run by
# gsrpi-dev-base's entrypoint, with bash and a time limit, after it has cloned or pulled
# /workspace and before it starts sshd. So the lock it installs from is the one the pull just
# brought, and no session can arrive to a half-installed venv. docker-login.dev.sh also runs
# it, after a clone that start-up could not make.
#
# A non-zero exit is printed by the entrypoint as a warning and the container still comes
# up: one you can ssh into and fix beats one that refused to start. So a failure here is
# reported, never swallowed, or a broken sync hides behind a container that looks fine until
# the first import fails.
set -u

rc=0

# The build put every dependency in /home/dev/venv without the project; this adds invective
# itself, editable, from the clone. On every start, because the venv is the image's and a
# rebuild is a fresh one.
#
# THE FLAGS MUST MATCH Dockerfile.dev's build sync (--all-extras): uv sync makes the venv
# match what it is asked for, so fewer flags here would uninstall the coverage extra.
# --frozen installs exactly what uv.lock says and never resolves, so the environment is not
# a function of when the container last restarted.
if [ -f /workspace/pyproject.toml ]; then
    if (cd /workspace && uv sync --frozen --all-extras); then
        echo "venv synced from uv.lock (--all-extras)"
    else
        rc=1
        echo "uv sync FAILED: /home/dev/venv is stale or incomplete."
        echo "    The container is still up: ssh in and run the same command to see why."
    fi
else
    echo "no clone in /workspace yet: venv left as built."
    echo "    The first clone needs the hub's token;"
    echo "    'make login C=invective-dev' on zero clones it."
fi

exit "$rc"
