# drone_firm_monitoring Renode image: Bitcraze's Renode (with the Crazyflie models), built,
# plus the Crazyflie firmware and its address profile. Every drone in `dfm up` is a
# container from this image.
#
#   docker build -f image/Dockerfile -t drone_firm_monitoring:dev .
#
# Every version is pinned, so two builds produce the same firmware (its sha256 is checked).
# If port 80 is blocked, use an HTTPS mirror:
#   --build-arg APT_MIRROR=https://mirrors.edge.kernel.org/ubuntu
#
# The buildpack-deps:jammy-curl base is ubuntu:jammy with ca-certificates, so apt can use
# HTTPS mirrors from the start.
ARG BASE=buildpack-deps:jammy-curl

FROM ${BASE} AS base
ARG APT_MIRROR=http://archive.ubuntu.com/ubuntu
ENV DEBIAN_FRONTEND=noninteractive
RUN printf 'deb %s jammy main universe\ndeb %s jammy-updates main universe\ndeb %s jammy-security main universe\n' \
      "$APT_MIRROR" "$APT_MIRROR" "$APT_MIRROR" > /etc/apt/sources.list

# ---------------------------------------------------------------------------
# 1. Crazyflie firmware (same compiler as bitcraze/builder)
FROM base AS firmware
ARG FW_REF=2026.08
ARG FW_SHA256=7c27a4cc2a255079cd9cceaf102032ba7954b842b757503a2f88cab836f8feed
RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc libc6-dev gcc-arm-none-eabi libnewlib-arm-none-eabi make python3 git gdb-multiarch \
 && rm -rf /var/lib/apt/lists/*
# /module, as in bitcraze/builder: the path ends up in the debug info and changes the sha256.
RUN git clone --recursive --branch "$FW_REF" https://github.com/bitcraze/crazyflie-firmware.git /module
WORKDIR /module
RUN make cf2_defconfig && make -j"$(nproc)"
RUN echo "$FW_SHA256  build/cf2.elf" | sha256sum -c -
COPY engine/scripts/firmware_profile.py /tmp/firmware_profile.py
RUN python3 /tmp/firmware_profile.py build/cf2.elf -o build/cf2.profile.json

# ---------------------------------------------------------------------------
# Mono, shared by the Renode build and the final image
FROM base AS mono
RUN apt-get update && apt-get install -y --no-install-recommends mono-complete libzmq5 python3 \
 && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# 2. Bitcraze's Renode (crazyflie branch), at the versions used by CF_renode_emulator
FROM mono AS renode
ARG RENODE_REF=975de43af3a91845a791126001c49626e3112ef1
ARG INFRA_REF=c9c51b2d80d92507f07018d7559a9621892c8ff3
ARG RESOURCES_REF=7b7d316924cb20ec887472252b373f31066e0c6d
RUN apt-get update && apt-get install -y --no-install-recommends git gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/bitcraze/renode.git /renode-cf \
 && git -C /renode-cf checkout -q "$RENODE_REF" \
 && git -C /renode-cf submodule update --init --recursive
WORKDIR /renode-cf
RUN git -C src/Infrastructure fetch -q origin "$INFRA_REF" \
 && git -C src/Infrastructure checkout -q -f "$INFRA_REF" \
 && git -C src/Infrastructure submodule update --init --recursive
# Pinned renode-resources. With the .renode_libs_fetched marker, build.sh only checks that a
# local master branch exists; without it, it would re-clone master, which lacks Lucene.Net.dll.
RUN git init -q lib/resources \
 && git -C lib/resources remote add origin https://github.com/renode/renode-resources.git \
 && git -C lib/resources fetch -q --depth=1 origin "$RESOURCES_REF" \
 && git -C lib/resources checkout -q -f FETCH_HEAD \
 && git -C lib/resources fetch -q --depth=1 origin master:master \
 && touch .renode_libs_fetched
RUN ./build.sh --no-gui

# ---------------------------------------------------------------------------
# 3. Final image: just what is needed to run Renode, plus the firmware and its profile
FROM mono
ARG FW_REF=2026.08
ARG FW_SHA256=7c27a4cc2a255079cd9cceaf102032ba7954b842b757503a2f88cab836f8feed
ARG RENODE_REF=975de43af3a91845a791126001c49626e3112ef1
COPY --from=renode /renode-cf/renode /renode-cf/renode
COPY --from=renode /renode-cf/tools /renode-cf/tools
COPY --from=renode /renode-cf/output /renode-cf/output
COPY --from=renode /renode-cf/platforms /renode-cf/platforms
COPY --from=renode /renode-cf/scripts /renode-cf/scripts
RUN mv /renode-cf/scripts/monitor.py /renode-cf/scripts/monitor.py.orig \
 && ln -sf /lib/x86_64-linux-gnu/libdl.so.2 /renode-cf/output/bin/Release/libdl.so
COPY --from=firmware /module/build/cf2.elf /module/build/cf2.profile.json /renode-cf/firmware/
# Defaults; dfm mounts the repo's monitor commands on top (/dfm/monitor_cf.py) and, for a
# drone with a different firmware, its ELF and profile (/dfm/firmware/).
COPY engine/scripts/monitor_cf.py /renode-cf/dfm/monitor_cf.py
COPY image/entrypoint.py /usr/local/bin/dfm-renode
LABEL org.opencontainers.image.title="drone_firm_monitoring" \
      org.opencontainers.image.description="Bitcraze Renode with the Crazyflie firmware, for drone_firm_monitoring" \
      org.opencontainers.image.licenses="MIT AND GPL-3.0-only" \
      dfm.firmware.ref="$FW_REF" dfm.firmware.sha256="$FW_SHA256" dfm.renode.ref="$RENODE_REF"
WORKDIR /renode-cf
EXPOSE 9999
ENTRYPOINT ["python3", "/usr/local/bin/dfm-renode"]
