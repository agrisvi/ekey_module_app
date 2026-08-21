#!/bin/bash
# ekey Blueprint Installation Script for Linux/Home Assistant OS
# This script copies blueprints to the correct Home Assistant directories
#
# Only AUTOMATION blueprints remain. The two script blueprints (enrol, delete) are
# gone: enrolment and deletion now happen in the ekey panel in the sidebar, which
# shows live progress and can assign a fingerprint enrolled on the device itself.

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}ekey Blueprint Installer${NC}"
echo "================================"
echo ""

# Detect Home Assistant config directory
if [ -d "/config" ]; then
    # Home Assistant OS/Container
    CONFIG_DIR="/config"
elif [ -d "$HOME/.homeassistant" ]; then
    # Home Assistant Core
    CONFIG_DIR="$HOME/.homeassistant"
elif [ -n "$HASS_CONFIG" ]; then
    # Custom config directory
    CONFIG_DIR="$HASS_CONFIG"
else
    echo "Could not find Home Assistant config directory."
    echo "Please specify manually:"
    read -p "Config directory path: " CONFIG_DIR
fi

echo "Using config directory: $CONFIG_DIR"
echo ""

# Find source blueprints
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
SOURCE_DIR="$SCRIPT_DIR/blueprints"

if [ ! -d "$SOURCE_DIR" ]; then
    echo "Error: Blueprints directory not found at $SOURCE_DIR"
    exit 1
fi

AUTO_DEST="$CONFIG_DIR/blueprints/automation/ekey"

echo "Creating blueprint directory..."
mkdir -p "$AUTO_DEST" || exit 1

# A loop rather than one cp per name, and a missing file is an ERROR. The previous
# version copied a blueprint that did not exist (door_unlock_on_match.yaml) and
# silently skipped one that did, because each cp hid its own failure.
BLUEPRINTS="toggle_relay_on_granted.yaml welcome_notification.yaml access_notification_list.yaml"

echo ""
echo "Copying automation blueprints..."
failed=0
for bp in $BLUEPRINTS; do
    if [ ! -f "$SOURCE_DIR/$bp" ]; then
        echo -e "${RED}✗${NC} $bp — not found in $SOURCE_DIR"
        failed=1
    elif cp "$SOURCE_DIR/$bp" "$AUTO_DEST/"; then
        echo -e "${GREEN}✓${NC} $bp"
    else
        echo -e "${RED}✗${NC} $bp — could not copy to $AUTO_DEST"
        failed=1
    fi
done

if [ "$failed" -ne 0 ]; then
    echo ""
    echo -e "${RED}Installation incomplete.${NC}"
    exit 1
fi

# Older copies elsewhere. This installs to one folder, but a blueprint imported
# through the UI lands wherever that import put it, and earlier releases of this
# project used other folder names. Home Assistant identifies a blueprint by its PATH,
# so an existing automation keeps using the copy it was created from — overwriting
# this folder does not touch it. The symptom is two blueprints with almost the same
# name and an automation whose entity picker is empty because it still refers to
# select.*_enrolled_fingerprints, which no longer exists. Naming the files is the
# difference between a two-minute fix and an afternoon.
# `IFS= read -r` throughout rather than `for f in $(find …)`: that splits on spaces,
# and a config path with a space in it would turn this warning into a pile of
# not-found errors.
stale=$(find "$CONFIG_DIR/blueprints/automation" -name '*.yaml' 2>/dev/null \
        | grep -v "^$AUTO_DEST/" \
        | while IFS= read -r f; do
              grep -qi 'ekey' "$f" 2>/dev/null && printf '%s\n' "$f"
          done)

if [ -n "$stale" ]; then
    echo ""
    echo -e "${BLUE}Other ekey blueprints found outside $AUTO_DEST:${NC}"
    printf '%s\n' "$stale" | while IFS= read -r f; do
        [ -z "$f" ] && continue
        if grep -q 'enrolled_fingerprints' "$f" 2>/dev/null; then
            echo -e "  ${RED}!${NC} $f  ${RED}(uses the removed select entity)${NC}"
        else
            echo "  - $f"
        fi
    done
    echo ""
    echo "  An automation created from one of these still uses THAT file, not the one"
    echo "  just installed. For each: open the automation, recreate it from the ekey"
    echo "  blueprint in $AUTO_DEST, then delete the old file."
fi

echo ""
echo -e "${GREEN}Installation complete!${NC}"
echo ""
echo "Next steps:"
echo "1. Go to Developer Tools → YAML → Reload Automations"
echo "2. Go to Settings → Automations & scenes → Blueprints"
echo "3. Click 'Create automation' on an ekey blueprint"
echo ""
echo "Users and fingerprints are managed in the ekey panel in the sidebar,"
echo "not by a blueprint. See QUICKSTART.md."
