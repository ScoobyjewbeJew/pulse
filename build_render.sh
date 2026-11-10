#!/usr/bin/env bash
set -euo pipefail

echo "Pulse build: node=$(node --version) npm=$(npm --version)"
node -e 'if (Number(process.versions.node.split(".")[0]) < 22) { throw new Error("Pulse token provider requires Node.js 22 or newer. Set NODE_VERSION=24.18.0 in Render."); }'

python -m pip install -r requirements.txt

provider=".runtime/bgutil-ytdlp-pot-provider"
# A clone interrupted by a failed build is otherwise reused forever and npm then
# reports a missing lock file, so only keep a checkout that is actually complete.
if [[ ! -f "$provider/server/package-lock.json" ]]; then
  echo "Cloning token provider 2.0.2"
  rm -rf "$provider"
  mkdir -p .runtime
  git -c advice.detachedHead=false clone --quiet --depth 1 --branch 2.0.2 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git "$provider"
fi

cd "$provider/server"
echo "Installing token provider dependencies"
npm ci --no-audit --no-fund
echo "Compiling token provider"
npx tsc
echo "Pulse build complete"
