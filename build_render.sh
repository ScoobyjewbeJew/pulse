#!/usr/bin/env bash
set -euo pipefail

python -m pip install -r requirements.txt
node -e 'if (Number(process.versions.node.split(".")[0]) < 22) { throw new Error("Pulse token provider requires Node.js 22 or newer. Set NODE_VERSION=24.18.0 in Render."); }'

provider_dir=".runtime/bgutil-ytdlp-pot-provider"
if [[ ! -d "$provider_dir" ]]; then
    git clone --depth 1 --branch 2.0.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git "$provider_dir"
fi

(
    cd "$provider_dir/server"
    npm ci
    npm exec -- tsc
)
