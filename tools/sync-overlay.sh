#!/bin/sh
#
# Keep openwrt-overlay/ and the openwrt/ submodule in step.
#
# openwrt/ is a git submodule, so nothing in openwrt-overlay/ reaches a build
# until it is copied across. That copy has been manual, and forgetting it
# costs a full build cycle chasing a change that was never in the image --
# or, worse, an edit made directly in openwrt/ that never gets committed
# because only openwrt-overlay/ is tracked by this repo.
#
# Four modes:
#   push             overlay -> openwrt/   (before building; the default)
#   pull             openwrt/ -> overlay   (only files already owned by overlay)
#   check            compare only, exit 1 on drift, print what differs
#   add <relpath>    start tracking an openwrt/<relpath> file in openwrt-overlay/
#
# "files/" is deliberately not synced: those are payloads baked into an image
# by the image recipe, not sources the build tree reads from a fixed path.

set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:-push}"

# overlay path : tree path, relative to REPO
PAIRS="
target/linux/econet:openwrt/target/linux/econet
package/kernel/econet-eth:openwrt/package/kernel/econet-eth
package/kernel/mt76:openwrt/package/kernel/mt76
"

die() { echo "sync-overlay: $*" >&2; exit 1; }

[ -d "$REPO/openwrt/target/linux" ] || die "openwrt/ is empty -- run: git submodule update --init"

if [ "$MODE" = add ]; then
	rel="${2#openwrt/}"
	[ -n "$rel" ] || die "usage: $0 add <path-relative-to-openwrt>"
	src="$REPO/openwrt/$rel"
	dst="$REPO/openwrt-overlay/$rel"
	[ -f "$src" ] || die "missing file in openwrt/: $src"
	mkdir -p "$(dirname "$dst")"
	cp -a "$src" "$dst"
	echo "add   $rel"
	exit 0
fi

drift=0

for pair in $PAIRS; do
	src="$REPO/openwrt-overlay/${pair%%:*}"
	dst="$REPO/${pair#*:}"

	[ -d "$src" ] || die "missing overlay dir: $src"
	mkdir -p "$dst"

	case "$MODE" in
	push)
		cp -a "$src/." "$dst/"
		echo "push  ${pair%%:*}"
		;;
	pull)
		# Only pull files that the overlay already owns, so unrelated
		# upstream boards/patches in openwrt/ are never copied into
		# openwrt-overlay/. Use '$0 add <relpath>' to track new files.
		for f in $(cd "$src" && find . -type f | sed 's|^\./||' | sort); do
			if [ -f "$dst/$f" ]; then
				cp -a "$dst/$f" "$src/$f"
			else
				echo "WARN  missing in openwrt/: ${pair%%:*}/$f" >&2
			fi
		done
		echo "pull  ${pair%%:*}"
		;;
	check)
		# Only compare files the overlay actually owns.
		for f in $(cd "$src" && find . -type f | sed 's|^\./||' | sort); do
			if ! cmp -s "$src/$f" "$dst/$f"; then
				echo "DRIFT ${pair%%:*}/$f"
				drift=$((drift + 1))
			fi
		done
		;;
	*)
		die "unknown mode '$MODE' (push|pull|check|add)"
		;;
	esac
done

if [ "$MODE" = check ]; then
	if [ "$drift" -gt 0 ]; then
		echo "sync-overlay: $drift file(s) differ -- run '$0 push' or '$0 pull'" >&2
		exit 1
	fi
	echo "sync-overlay: overlay and build tree agree"
fi

case "$MODE" in
push)
	echo
	echo "Note: target base-files changes need an explicit rebuild before"
	echo "they reach an image:"
	echo "  make package/base-files/{clean,compile} && make package/install && make target/install"
	;;
esac
