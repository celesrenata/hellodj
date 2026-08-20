# Custom Lavalink image with fMP4 HLS support + pre-baked plugins
# Uses our custom-built Lavalink JAR (with lavaplayer fMP4 HLS patch)
# instead of the official image.
FROM eclipse-temurin:21-jre

WORKDIR /opt/Lavalink

COPY Lavalink.jar /opt/Lavalink/Lavalink.jar
COPY plugins/ /opt/Lavalink/plugins/

EXPOSE 2333

ENTRYPOINT ["java", "-Djdk.tls.client.protocols=TLSv1.1,TLSv1.2", "-jar", "Lavalink.jar"]
