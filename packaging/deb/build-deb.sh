#!/usr/bin/env bash
# Build a Debian/Ubuntu package: dist/rodecaster-linux_<version>_all.deb
#
# Works on Debian/Ubuntu (uses dpkg-deb) and on other distros (falls back to ar + tar,
# which is all a .deb is). Run from anywhere:  packaging/deb/build-deb.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PKG=rodecaster-linux
UPSTREAM="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml")"
DEBREV="${DEBREV:-1}"
VERSION="$UPSTREAM-$DEBREV"
OUT="$ROOT/dist"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

D="$STAGE/root"
PYDIR="$D/usr/lib/python3/dist-packages"
mkdir -p "$PYDIR" "$D/usr/bin" "$D/DEBIAN"

# Python package (pure Python, so the version-independent dist-packages dir)
cp -r "$ROOT/rodecaster" "$PYDIR/"
find "$PYDIR" -name __pycache__ -prune -exec rm -rf {} +

# Launchers (equivalent to the pyproject entry points)
cat > "$D/usr/bin/rodecaster" <<'EOF'
#!/usr/bin/python3
import sys
from rodecaster.ui.app import main
sys.exit(main())
EOF
cat > "$D/usr/bin/rodecaster-cli" <<'EOF'
#!/usr/bin/python3
import sys
from rodecaster.cli import main
sys.exit(main())
EOF

install -Dm644 "$ROOT/data/70-rodecaster-pro.rules" "$D/usr/lib/udev/rules.d/70-rodecaster-pro.rules"
install -Dm644 "$ROOT/data/io.github.rodecaster_linux.RodeCasterPro.desktop" \
  "$D/usr/share/applications/io.github.rodecaster_linux.RodeCasterPro.desktop"
install -Dm644 "$ROOT/data/io.github.rodecaster_linux.RodeCasterPro.svg" \
  "$D/usr/share/icons/hicolor/scalable/apps/io.github.rodecaster_linux.RodeCasterPro.svg"
install -Dm644 "$ROOT/README.md" "$D/usr/share/doc/$PKG/README.md"
install -Dm644 "$ROOT/PROTOCOL.md" "$D/usr/share/doc/$PKG/PROTOCOL.md"
{
  echo "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/"
  echo "Upstream-Name: $PKG"
  echo "Source: https://github.com/ClosedNetwork/rodecaster-linux"
  echo
  echo "Files: *"
  echo "Copyright: $(sed -n 's/^Copyright (c) //p' "$ROOT/LICENSE")"
  echo "License: MIT"
  sed -n '/^Permission/,$p' "$ROOT/LICENSE" | sed 's/^$/./; s/^/ /'
} > "$D/usr/share/doc/$PKG/copyright"

chmod 755 "$D/usr/bin/"*
find "$D" -type d -exec chmod 755 {} +
find "$D/usr/lib" "$D/usr/share" -type f -exec chmod 644 {} +

SIZE_KB="$(du -sk --exclude=DEBIAN "$D" | cut -f1)"
cat > "$D/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Architecture: all
Maintainer: Simon <simon@closednetwork.io>
Installed-Size: $SIZE_KB
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.5), ffmpeg
Recommends: udisks2
Section: sound
Priority: optional
Homepage: https://github.com/ClosedNetwork/rodecaster-linux
Description: Unofficial Linux companion app for the original RØDECaster Pro (gen 1)
 GTK4/libadwaita app and command-line tool for the first-generation RØDECaster
 Pro: soundboard pad upload/download/naming/colours, bank names, Podcast
 Transfer Mode with recording export, effects and settings backup.
 .
 Not affiliated with RØDE Microphones.
EOF

cat > "$D/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ] && command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules || true
    udevadm trigger --subsystem-match=hidraw --action=change || true
fi
EOF
cat > "$D/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = remove ] && command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules || true
fi
EOF
cat > "$D/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
# Remove bytecode Python may have written at runtime
rm -rf /usr/lib/python3/dist-packages/rodecaster/__pycache__ \
       /usr/lib/python3/dist-packages/rodecaster/ui/__pycache__
EOF
chmod 755 "$D/DEBIAN/postinst" "$D/DEBIAN/postrm" "$D/DEBIAN/prerm"
(cd "$D" && find . -path ./DEBIAN -prune -o -type f -printf '%P\0' | sort -z | xargs -0 md5sum) > "$D/DEBIAN/md5sums"
chmod 644 "$D/DEBIAN/control" "$D/DEBIAN/md5sums"

mkdir -p "$OUT"
DEB="$OUT/${PKG}_${VERSION}_all.deb"
rm -f "$DEB"
if command -v dpkg-deb >/dev/null 2>&1; then
    dpkg-deb --root-owner-group -Zxz --build "$D" "$DEB" >/dev/null
else
    T="$STAGE/ar"; mkdir -p "$T"
    TAR=(tar --owner=0 --group=0 --numeric-owner --sort=name --format=gnu)
    (cd "$D/DEBIAN" && "${TAR[@]}" -cJf "$T/control.tar.xz" .)
    (cd "$D" && "${TAR[@]}" --exclude=./DEBIAN -cJf "$T/data.tar.xz" .)
    echo "2.0" > "$T/debian-binary"
    # Write the ar container by hand: same header format dpkg-deb emits (no GNU '/' suffix).
    ar_member() {
        local size; size=$(stat -c %s "$1")
        printf '%-16s%-12s%-6s%-6s%-8s%-10s`\n' "$(basename "$1")" "$(date +%s)" 0 0 100644 "$size"
        cat "$1"
        if (( size % 2 )); then printf '\n'; fi
    }
    { printf '!<arch>\n'
      ar_member "$T/debian-binary"; ar_member "$T/control.tar.xz"; ar_member "$T/data.tar.xz"
    } > "$DEB"
fi
echo "$DEB"
