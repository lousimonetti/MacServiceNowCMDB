#!/bin/sh
# Intune macOS custom attribute: physical RAM in bytes.
#
# Graph reports physicalMemoryInBytes as 0 for Macs, so this is where
# intune-cmdb-sync gets their RAM (INTUNE_MAC_RAM_ATTRIBUTE). Upload it under
# Devices > macOS > Custom attributes with data type Integer; see
# docs/field-mapping.md, "Mac RAM from a custom attribute".
#
# Intune keeps stdout as the attribute's value, and treats a non-zero exit as a
# failed run, which the connector ignores. So print exactly one integer, or fail.

memsize=$(/usr/sbin/sysctl -n hw.memsize 2>/dev/null)

case "$memsize" in
  ''|*[!0-9]*) exit 1 ;;
esac

echo "$memsize"
