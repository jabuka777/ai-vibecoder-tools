#!/usr/bin/env bash
# Скачивает Zoom Meeting SDK Web и кладёт его в static/ (около 160 МБ, в git не хранится).
set -euo pipefail
VER="${ZOOM_SDK_VERSION:-6.10.0}"
cd "$(dirname "$0")"
tmp=$(mktemp -d)
( cd "$tmp" && npm pack "@zoom/meetingsdk@$VER" -q >/dev/null && tar xzf zoom-meetingsdk-"$VER".tgz )
rm -rf static/lib
cp -r "$tmp/package/dist/lib" static/
cp "$tmp/package/dist/zoom-meeting-$VER.min.js" static/
rm -rf "$tmp"
sed -i "s/zoom-meeting-[0-9.]*\.min\.js/zoom-meeting-$VER.min.js/" static/index.html
echo "Zoom Meeting SDK $VER → static/"
