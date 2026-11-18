FROM batfish/batfish:2025.07.07.2423@sha256:5515157739b7dca9a63b04e7a4a8f9c3d94ab0bdb8cd79ece57a5a5204307fbb AS upstream

FROM eclipse-temurin:17-jdk@sha256:fe947d993c32dd9cf651a4353a6ed723c2370b3a9d034ea4cab4775404ea531a AS compiler
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends patch && rm -rf /var/lib/apt/lists/*
COPY --from=upstream /allinone-bundle.jar ./
ADD https://raw.githubusercontent.com/batfish/batfish/7a0efda8257058576ae3787925e8c720dc918820/projects/batfish/src/main/java/org/batfish/representation/frr/FrrConversions.java ./FrrConversions.java
COPY frr-next-hop-self.patch ./
RUN patch --batch --fuzz=0 FrrConversions.java < frr-next-hop-self.patch \
    && javac -proc:none -cp allinone-bundle.jar -d classes FrrConversions.java \
    && jar uf allinone-bundle.jar -C classes .

FROM upstream
COPY --from=compiler /build/allinone-bundle.jar /allinone-bundle.jar
LABEL org.nika.batfish-patch="frr-next-hop-self-ebgp-to-ibgp"
