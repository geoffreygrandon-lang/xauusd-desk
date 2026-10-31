#!/bin/bash
# Prépare le dossier servi par GitHub Pages.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf _site
mkdir -p _site/data
cp web/index.html web/manifest.webmanifest web/sw.js web/logo.svg web/favicon.png \
  web/icon-180.png web/icon-192.png web/icon-512.png web/icon-maskable-512.png _site/
cp data/latest.json _site/data/latest.json
cp data/macro.json _site/data/macro.json
touch _site/.nojekyll
echo "Site assemblé dans _site/"
