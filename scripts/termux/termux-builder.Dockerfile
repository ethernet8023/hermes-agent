# Derived build image: the digest-pinned termux-docker base (same pin as
# pm/lock.json's termux-docker row -- build_builder_image.sh injects it via
# --build-arg) with the wheelhouse build toolchain pre-installed.
#
# The provisioning block below mirrors the in-container half of
# termux_build.sh EXACTLY (official-mirror pin, package list): the runtime
# probe there (`command -v clang ...`) makes the baked toolchain a no-op,
# so this image is a drop-in replacement that skips the ~10-minute
# per-run toolchain apt on arm runners.
#
# The termux image has no /bin/sh (termux's shell lives at $PREFIX/bin/sh),
# so every RUN goes through the termux bash explicitly.
ARG BASE
FROM ${BASE}

USER root
SHELL ["/data/data/com.termux/files/usr/bin/bash", "-c"]
ENV PREFIX=/data/data/com.termux/files/usr
ENV PATH="${PREFIX}/bin:/usr/bin:/bin"
ENV DEBIAN_FRONTEND=noninteractive

RUN printf '%s\n' "deb https://packages.termux.dev/apt/termux-main stable main" \
      > "${PREFIX}/etc/apt/sources.list" \
 && rm -f "${PREFIX}/etc/apt/sources.list.d/"*.list \
 && (apt update || apt update) \
 && apt install -y \
      clang rust make patchelf binutils pkg-config protobuf cmake ninja \
      autoconf automake libtool \
      libandroid-posix-semaphore libandroid-support libbz2 libffi \
      libjpeg-turbo libpng freetype libtiff libwebp openjpeg littlecms \
      libyaml openssl readline zlib liblzma libsqlite ncurses \
 && mkdir -p /bin \
 && ln -sf "${PREFIX}/bin/sh" /bin/sh
