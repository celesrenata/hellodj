# Custom Lavalink image with pre-baked plugins (SABR-capable youtube-source + lavasrc)
# Based on the official Lavalink image but replaces Maven-downloaded plugins with
# pre-built jars that include SABR (Server ABR) streaming support.
FROM ghcr.io/lavalink-devs/lavalink:4

# Remove any default plugins and add our custom ones
RUN rm -rf /opt/Lavalink/plugins/*
COPY plugins/ /opt/Lavalink/plugins/
