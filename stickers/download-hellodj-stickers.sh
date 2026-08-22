#!/usr/bin/env bash
set -euo pipefail

#
# download-hellodj-stickers.sh
#
# Downloads latest upstream sticker/emoji collections for HelloDJ:
#
#   - Google Noto Animated Emoji       -> GIF
#   - Microsoft Fluent Emoji Animated  -> APNG
#   - Kenney Emotes                    -> PNG/etc
#   - Microsoft Fluent Emoji           -> PNG/SVG
#   - OpenMoji                         -> PNG/SVG
#   - Blobmoji                         -> PNG/SVG
#   - Twemoji                          -> PNG/SVG
#
# Requirements:
#   curl jq git git-lfs unzip zip
#
# Usage:
#   ./download-hellodj-stickers.sh
#
# Optional:
#   OUT=/path/to/stickers ./download-hellodj-stickers.sh
#

OUT="${OUT:-./stickers-upstream}"
PARALLEL="${PARALLEL:-8}"

mkdir -p "$OUT"
cd "$OUT"

need() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "ERROR: required command not found: $1" >&2
        exit 1
    }
}

for cmd in curl jq git git-lfs unzip zip; do
    need "$cmd"
done

git lfs install --skip-repo >/dev/null

echo "============================================================"
echo " HelloDJ sticker upstream downloader"
echo " Output: $(pwd)"
echo "============================================================"

github_latest_release_zip() {
    local repo="$1"
    local output="$2"

    echo "Resolving latest release: $repo"

    local api="https://api.github.com/repos/${repo}/releases/latest"
    local tag
    tag="$(curl -fsSL "$api" | jq -r '.tag_name')"

    if [[ -z "$tag" || "$tag" == "null" ]]; then
        echo "ERROR: Could not determine latest release for $repo"
        return 1
    fi

    echo "  -> $tag"

    curl -fL \
        --retry 3 \
        -o "$output" \
        "https://github.com/${repo}/archive/refs/tags/${tag}.zip"
}

github_head_zip() {
    local repo="$1"
    local branch="$2"
    local output="$3"

    echo "Downloading latest $repo/$branch"

    curl -fL \
        --retry 3 \
        -o "$output" \
        "https://github.com/${repo}/archive/refs/heads/${branch}.zip"
}

echo
echo "=== Google Noto Animated Emoji ==="

NOTO_DIR="Noto-Animated"
rm -rf "$NOTO_DIR"
mkdir -p "$NOTO_DIR"

curl -fsSL \
    "https://googlefonts.github.io/noto-emoji-animation/data/api.json" \
    -o "$NOTO_DIR/api.json"

jq -r '.icons[].codepoint' \
    "$NOTO_DIR/api.json" \
    > "$NOTO_DIR/codepoints.txt"

NOTO_COUNT="$(wc -l < "$NOTO_DIR/codepoints.txt")"
echo "Found $NOTO_COUNT animated emoji"

export NOTO_DIR

cat "$NOTO_DIR/codepoints.txt" |
    xargs -P "$PARALLEL" -I '{}' bash -c '
        code="$1"
        printf "  Noto %-32s\r" "$code"

        curl -fsSL \
            --retry 3 \
            "https://fonts.gstatic.com/s/e/notoemoji/latest/${code}/512.gif" \
            -o "${NOTO_DIR}/${code}.gif" \
            || echo "WARNING: failed Noto $code" >&2
    ' _ '{}'

echo
echo "Packing Noto animated GIFs..."
rm -f "Noto-Animated-Latest.zip"
(
    cd "$NOTO_DIR"
    zip -q -r ../Noto-Animated-Latest.zip .
)

echo "  -> Noto-Animated-Latest.zip"

echo
echo "=== Microsoft Fluent Emoji Animated ==="
echo "NOTE: This one is roughly 5 GB upstream."

FLUENT_ANIM="Fluent-Emoji-Animated"
rm -rf "$FLUENT_ANIM"

GIT_LFS_SKIP_SMUDGE=1 git clone \
    --depth 1 \
    https://github.com/microsoft/fluentui-emoji-animated.git \
    "$FLUENT_ANIM"

(
    cd "$FLUENT_ANIM"
    echo "Downloading Git LFS animation objects..."
    git lfs pull
    git rev-parse HEAD > UPSTREAM_COMMIT.txt
)

echo "  -> $FLUENT_ANIM/"

echo
echo "=== Kenney Emotes Pack ==="

KENNEY_PAGE="$(
    curl -fsSL \
        "https://kenney.nl/assets/emotes-pack"
)"

KENNEY_URL="$(
    printf '%s' "$KENNEY_PAGE" |
        grep -oE '(https://kenney\.nl)?/media/pages/assets/emotes-pack/[^"'\'' ]+/kenney_emotes-pack\.zip' |
        head -1
)"

if [[ -z "$KENNEY_URL" ]]; then
    echo "ERROR: Could not discover Kenney Emotes download URL" >&2
else
    if [[ "$KENNEY_URL" == /* ]]; then
        KENNEY_URL="https://kenney.nl${KENNEY_URL}"
    fi

    echo "Downloading:"
    echo "  $KENNEY_URL"

    curl -fL \
        --retry 3 \
        "$KENNEY_URL" \
        -o Kenney-Emotes-Latest.zip

    echo "  -> Kenney-Emotes-Latest.zip"
fi

echo
echo "=== Microsoft Fluent Emoji Static ==="

github_head_zip \
    "microsoft/fluentui-emoji" \
    "main" \
    "Fluent-Emoji-Static-Latest.zip"

echo
echo "=== OpenMoji ==="

github_latest_release_zip \
    "hfg-gmuend/openmoji" \
    "OpenMoji-Latest.zip"

echo
echo "=== Blobmoji ==="

github_head_zip \
    "C1710/blobmoji" \
    "main" \
    "Blobmoji-Latest.zip" \
    || \
github_head_zip \
    "C1710/blobmoji" \
    "master" \
    "Blobmoji-Latest.zip"

echo
echo "=== Twemoji ==="

github_latest_release_zip \
    "jdecked/twemoji" \
    "Twemoji-Latest.zip"

echo
echo "============================================================"
echo " Finished"
echo "============================================================"
echo

du -sh \
    Noto-Animated-Latest.zip \
    Fluent-Emoji-Animated \
    Kenney-Emotes-Latest.zip \
    Fluent-Emoji-Static-Latest.zip \
    OpenMoji-Latest.zip \
    Blobmoji-Latest.zip \
    Twemoji-Latest.zip \
    2>/dev/null || true

echo
echo "Files are under:"
echo "  $(pwd)"
