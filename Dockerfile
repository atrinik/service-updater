# Packaging happens in GitHub Actions. Deployment hosts pull this image.
FROM node@sha256:64af3819f9275802414d7cdc38c27e9d82bd564dec4d4da87d008255d36c63b4 AS verifier
WORKDIR /build
ADD --checksum=sha256:bb766f710eef8ede859c18578c72c327597cd4c8a85b06001b1f3843c6019386 https://github.com/cli/cli/releases/download/v2.102.0/gh_2.102.0_linux_amd64.tar.gz /build/gh.tar.gz
RUN tar -xzf gh.tar.gz && mkdir /out && cp gh_2.102.0_linux_amd64/bin/gh /out/gh && cp gh_2.102.0_linux_amd64/LICENSE /out/GITHUB-CLI-LICENSE
# The pinned CLI validates current TUF metadata using its embedded trusted roots.
# No credentials are provided. A stale/invalid trust chain fails image packaging.
RUN env -i PATH=/usr/bin:/bin HOME=/tmp /out/gh attestation trusted-root > /out/trusted-root.jsonl && test -s /out/trusted-root.jsonl
FROM scratch
LABEL org.opencontainers.image.source="https://github.com/atrinik/service-updater" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.title="Atrinik service updater"
COPY --from=verifier /out/gh /usr/local/bin/gh
COPY --from=verifier /out/trusted-root.jsonl /trusted-root.jsonl
COPY --from=verifier /out/GITHUB-CLI-LICENSE /licenses/GITHUB-CLI-LICENSE
COPY service_updater.py core.py LICENSE README.md /updater/
COPY adapters /updater/adapters/
COPY config /updater/config/
COPY docs /updater/docs/
USER 65532:65532
ENV HOME=/tmp GH_CONFIG_DIR=/tmp/gh
ENTRYPOINT ["/usr/local/bin/gh"]
