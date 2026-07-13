#!/bin/bash
# Re-render the guide PDFs from their HTML. Run this after editing either .html.
# ponytail: headless Chrome, because the HTML is already print-styled (@page letter,
# print-color-adjust). A real PDF lib would mean re-doing the layout.
set -e
cd "$(dirname "$0")"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
[ -x "$CHROME" ] || CHROME="$(command -v chromium || command -v google-chrome)"
[ -x "$CHROME" ] || { echo "No Chrome/Chromium found — install Chrome or set CHROME."; exit 1; }

for f in OpenLease-Setup-Guide OpenLease-From-Scratch; do
  "$CHROME" --headless --disable-gpu --no-pdf-header-footer \
    --virtual-time-budget=20000 \
    --print-to-pdf="$PWD/$f.pdf" "file://$PWD/$f.html" 2>/dev/null
  echo "built $f.pdf"
done
