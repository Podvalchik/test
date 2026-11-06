#!/usr/bin/env bash
# Сборка PDF из concept.md: pandoc -> HTML -> Chromium (Playwright) -> PDF
set -euo pipefail
cd "$(dirname "$0")/.."
pandoc concept.md -s --metadata lang=ru -c build/style.css --embed-resources -o build/concept.html
node build/topdf.js build/concept.html "Диво-остров_концепция_проекта.pdf"
