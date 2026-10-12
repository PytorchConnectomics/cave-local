FROM golang@sha256:1ae0735f00daffa3aaf1363a5184c0d2dc55c78e3db4ec70241cdac97bf84b59 AS build
WORKDIR /src
COPY vendor/emulators/bigtable/ ./
ENV CGO_ENABLED=0 GOMAXPROCS=4
RUN go build -o /cbtemulator ./cmd/cbtemulator
FROM alpine@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8
RUN apk add --no-cache ca-certificates
COPY --from=build /cbtemulator /usr/local/bin/cbtemulator
ENTRYPOINT ["cbtemulator", "-host", "0.0.0.0", "-port", "9000", "-dir", "/data"]
