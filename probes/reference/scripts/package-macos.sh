#!/bin/bash
# SPDX-License-Identifier: MIT
# Signs a probe .vst3 bundle (Developer ID, hardened runtime), builds a .pkg that installs it into
# /Library/Audio/Plug-Ins/VST3, notarizes and staples it.
#
#   SIGN_APP="Developer ID Application: …" SIGN_INSTALLER="Developer ID Installer: …" \
#   NOTARY_KEY=/path/AuthKey_XXXX.p8 NOTARY_KEY_ID=XXXX NOTARY_ISSUER=uuid \
#   scripts/package-macos.sh <Bundle.vst3> <pkg identifier> <version> <out dir>
#
# Without NOTARY_KEY the pkg is signed but not notarized. NOTARY_PROFILE (a notarytool keychain
# profile) can be used instead of the key variables.
set -euo pipefail

BUNDLE=${1:?bundle}; PKGID=${2:?pkg identifier}; VERSION=${3:?version}; OUT=${4:?out dir}
: "${SIGN_APP:?SIGN_APP: Developer ID Application identity}"
: "${SIGN_INSTALLER:?SIGN_INSTALLER: Developer ID Installer identity}"
NAME=$(basename "$BUNDLE" .vst3)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$OUT" "$WORK/root"

ditto --norsrc --noextattr "$BUNDLE" "$WORK/root/$NAME.vst3"
xattr -cr "$WORK/root/$NAME.vst3"
codesign --force --timestamp --options runtime --sign "$SIGN_APP" "$WORK/root/$NAME.vst3"
codesign --verify --strict --deep --verbose=2 "$WORK/root/$NAME.vst3"

# never relocate: install exactly into /Library/Audio/Plug-Ins/VST3 even if another copy exists
pkgbuild --analyze --root "$WORK/root" "$WORK/components.plist" >/dev/null
/usr/libexec/PlistBuddy -c "Delete :0:BundleIsRelocatable" "$WORK/components.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :0:BundleIsRelocatable bool false" "$WORK/components.plist"
# always replace the installed copy, also with a build of the same version
/usr/libexec/PlistBuddy -c "Set :0:BundleIsVersionChecked false" "$WORK/components.plist"
pkgbuild --root "$WORK/root" --component-plist "$WORK/components.plist" \
    --install-location /Library/Audio/Plug-Ins/VST3 --identifier "$PKGID" --version "$VERSION" \
    "$WORK/component.pkg" >/dev/null
PKG="$OUT/$NAME-$VERSION.pkg"
productbuild --package "$WORK/component.pkg" --sign "$SIGN_INSTALLER" --timestamp "$PKG" >/dev/null
pkgutil --check-signature "$PKG" | head -3

if [ -n "${NOTARY_KEY:-}" ] || [ -n "${NOTARY_PROFILE:-}" ]; then
    if [ -n "${NOTARY_PROFILE:-}" ]; then
        xcrun notarytool submit "$PKG" --keychain-profile "$NOTARY_PROFILE" --wait
    else
        xcrun notarytool submit "$PKG" --key "$NOTARY_KEY" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER" --wait
    fi
    xcrun stapler staple "$PKG"
    spctl -a -vv -t install "$PKG"
fi
# the signed bundle too, for manual installs
ditto -c -k --keepParent "$WORK/root/$NAME.vst3" "$OUT/$NAME-$VERSION.vst3.zip"
( cd "$OUT" && shasum -a 256 "$NAME-$VERSION.pkg" "$NAME-$VERSION.vst3.zip" > "SHA256SUMS-$NAME-$VERSION" )
echo "done: $PKG"
