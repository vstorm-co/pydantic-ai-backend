FROM rust:1.100.0-bookworm AS build
WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --release --locked

FROM gcr.io/distroless/cc-debian12:nonroot
COPY --from=build /build/target/release/projectfs-export /projectfs-export
COPY NOTICE.nfsembed /usr/share/licenses/projectfs/NOTICE.nfsembed
USER 65532:65532
EXPOSE 2049/tcp
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 CMD ["/projectfs-export", "healthcheck"]
ENTRYPOINT ["/projectfs-export"]
