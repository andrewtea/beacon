#!/usr/bin/env bash
# Regenerates the PWA icons in web/public from web/public/favicon.svg.
# Run it after changing the favicon. Needs ImageMagick 7 (`magick`).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../web/public"

brand_color="#0b1f33"

# Rounded icons keep the favicon's transparent corners.
rounded() {
  magick -background none -density 384 favicon.svg -resize "$1x$1" -depth 8 "$2"
}

# iOS and maskable icons must fill the whole square, so the corners are
# flattened onto the brand color and the platform applies its own mask.
square() {
  magick -background "$brand_color" -density 384 favicon.svg -flatten \
    -resize "$1x$1" -depth 8 "$2"
}

rounded 192 pwa-192x192.png
rounded 512 pwa-512x512.png
square 180 apple-touch-icon.png
square 512 maskable-icon-512x512.png
