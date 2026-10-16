#!/bin/sh
dir=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
node="${HOME}/.nvm/versions/node/v24.21.0/bin/node"
/usr/bin/nohup "$node" "$dir/m7-launch-browser.cjs" "$1" >> "$M7_BROWSER_RUNTIME/browser.log" 2>&1 < /dev/null &
exit 0
