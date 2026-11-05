set -eu
umask 022
cp -R /input/root /tmp/root
chown -R 0:0 /tmp/root
find /tmp/root -type d -exec chmod 0755 {} +
find /tmp/root -exec touch -d @0 {} +
exec apk mkpkg --compat 3.0.5 --compression deflate:9 --files /tmp/root "$@"
