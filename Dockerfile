# syntax=docker/dockerfile:1

FROM ghcr.io/linuxserver-playground/baseimage-alpine:3.22

# set version label
ARG BUILD_DATE
ARG VERSION
LABEL build_version="Linuxserver.io version:- ${VERSION} Build-date:- ${BUILD_DATE}"
LABEL maintainer="thelamer"

COPY pyproject.toml README.md LICENSE /tmp/app/
COPY control /tmp/app/control

RUN \
  echo "**** install build packages ****" && \
  apk add --no-cache --virtual=build-dependencies \
    build-base \
    git \
    libffi-dev \
    openssl-dev \
    python3-dev && \
  echo "**** install runtime packages ****" && \
  apk add --no-cache \
    jq \
    python3 && \
  echo "**** install control ****" && \
  python3 -m venv /lsiopy && \
  /lsiopy/bin/pip install -U --no-cache-dir \
    pip \
    wheel && \
  # lsio-builder (the contracts package) declares python >=3.13 but runs on the 3.12 Alpine ships
  /lsiopy/bin/pip install -U --no-cache-dir --ignore-requires-python \
    /tmp/app && \
  printf "Linuxserver.io version: ${VERSION}\nBuild-date: ${BUILD_DATE}\n" > /build_version && \
  echo "**** cleanup ****" && \
  apk del --purge \
    build-dependencies && \
  rm -rf \
    /root/.cache \
    /tmp/*

# add local files
COPY root/ /
