#!/usr/bin/env bash
# Сборка PDF из Markdown: pandoc -> HTML -> Chromium (Playwright) -> PDF
set -euo pipefail
cd "$(dirname "$0")/.."
build() {
  pandoc "$1" -s --metadata lang=ru -c build/style.css --embed-resources -o build/tmp.html
  node build/topdf.js build/tmp.html "$2"
}
build concept-short.md "Диво-остров_кратко.pdf"
build concept.md "Диво-остров_концепция_проекта.pdf"
rm -f build/tmp.html
