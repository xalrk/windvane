#!/bin/sh
# A first run on a machine that has never seen windvane: tests/test_clean_machine.py (install.sh into an empty home,
# doctor, install, init, new, and a run that merges the ticket, with a fake `claude`) in a throwaway podman or docker
# container from an official Debian image pinned by digest. First, install.sh on python3 alone must name the package
# a venv needs. By hand only, never in the check: it downloads the image and Debian's git, python3 and python3-venv.
# Usage: tools/clean_machine_check.sh           the test, in a new container
#        tools/clean_machine_check.sh --shell   a shell in one with windvane installed, for the run with the real claude
set -eu
cd "$(dirname "$0")/.."
image="docker.io/library/debian:trixie-20260918-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a"
engine=$(command -v podman || command -v docker) || { echo "needs podman or docker"; exit 1; }
apt='apt-get update -qq && apt-get install -qqy --no-install-recommends ca-certificates git python3 >/dev/null'
venv='apt-get install -qqy --no-install-recommends python3-venv >/dev/null'

if [ "${1:-}" = "--shell" ]; then
    exec "$engine" run --rm -it -e DEBIAN_FRONTEND=noninteractive -v "$PWD:/src:ro,Z" "$image" sh -c "$apt && $venv && sh /src/install.sh &&
        export PATH=\"\$HOME/.local/bin:\$PATH\" && cat <<'EOF'

windvane is installed. The last step, with the real claude:
  apt-get install -qqy curl && curl -fsSL https://claude.ai/install.sh | bash && export PATH=\"\$HOME/.local/bin:\$PATH\"
  claude auth login
  git config --global user.name me && git config --global user.email me@example.com && git config --global init.defaultBranch main
  git init -q --bare /tmp/remote.git && git clone -q /tmp/remote.git /tmp/hello && cd /tmp/hello
  echo hi > README && git add README && git commit -qm init && git push -q -u origin main
  windvane doctor && windvane init --check 'test -f README'
  windvane new 'Say hello in hello.txt' && git add .windvane && git commit -qm tickets && git push -q
  windvane run --for 1h
Leave with exit: the container goes with everything in it.
EOF
        exec bash"
fi

"$engine" run --rm -v "$PWD:/src:ro,Z" -e DEBIAN_FRONTEND=noninteractive -e PYTHONDONTWRITEBYTECODE=1 "$image" sh -c "set -e; $apt
    echo '--- install.sh on python3 without its venv module'
    if sh /src/install.sh >/tmp/out 2>&1; then cat /tmp/out; echo 'FAIL: install.sh passed without a venv module'; exit 1; fi
    cat /tmp/out
    grep -q python3-venv /tmp/out || { echo 'FAIL: install.sh never named python3-venv'; exit 1; }
    $venv
    echo \"--- tests/test_clean_machine.py on \$(python3 --version), git \$(git --version | cut -d' ' -f3)\"
    cd /src && python3 -m unittest -v tests.test_clean_machine"
echo "passed in a clean container. The last step with the real claude: tools/clean_machine_check.sh --shell"
