#!/bin/sh
# Intune macOS custom attribute: processor name ("Apple M5", or the full Intel
# brand string), mapped to cpu_name.
#
# Graph has no CPU name for any platform, so this is where intune-cmdb-sync
# gets it (INTUNE_MAC_CPU_ATTRIBUTE). Upload it under Devices > macOS > Custom
# attributes with data type String; see docs/field-mapping.md, "Mac CPU name
# from a custom attribute".
#
# Intune keeps stdout as the attribute's value. Print one line, or fail.

name=$(/usr/sbin/sysctl -n machdep.cpu.brand_string 2>/dev/null)

[ -n "$name" ] || exit 1

echo "$name"
