# Maintainer: Simon <simon@closednetwork.io>
# Build from this directory:  makepkg -si
pkgname=rodecaster-linux
pkgver=0.1.0
pkgrel=3
pkgdesc="Unofficial Linux companion app for the original RØDECaster Pro (gen 1)"
arch=('any')
url="https://github.com/ClosedNetwork/rodecaster-linux"
license=('MIT')
depends=('python' 'python-gobject' 'gtk4' 'libadwaita' 'ffmpeg')
optdepends=('udisks2: mount the microSD card in Podcast Transfer Mode')
makedepends=('python-build' 'python-installer' 'python-setuptools' 'python-wheel')
checkdepends=('python-pytest')
source=()
sha256sums=()

_srcdir() { echo "${startdir}"; }

build() {
  cd "$(_srcdir)"
  rm -rf dist
  python -m build --wheel --no-isolation
}

check() {
  cd "$(_srcdir)"
  python -m pytest -q
}

package() {
  cd "$(_srcdir)"
  python -m installer --destdir="$pkgdir" dist/*.whl
  install -Dm644 data/70-rodecaster-pro.rules "$pkgdir/usr/lib/udev/rules.d/70-rodecaster-pro.rules"
  install -Dm644 data/io.github.rodecaster_linux.RodeCasterPro.desktop \
    "$pkgdir/usr/share/applications/io.github.rodecaster_linux.RodeCasterPro.desktop"
  install -Dm644 data/io.github.rodecaster_linux.RodeCasterPro.svg \
    "$pkgdir/usr/share/icons/hicolor/scalable/apps/io.github.rodecaster_linux.RodeCasterPro.svg"
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
  install -Dm644 PROTOCOL.md "$pkgdir/usr/share/doc/$pkgname/PROTOCOL.md"
  install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
}
