# Bootstrap image: docker CLI + kind + kubectl. Runs cluster-up.sh against the
# host Docker daemon (socket mounted by docker compose) and exits.
FROM docker:29-cli

ARG KIND_VERSION=v0.33.0
ARG KUBECTL_VERSION=v1.37.0
ARG TARGETARCH

# Downloads are checksum-verified and retried (slow or flaky links reset
# connections mid-transfer).
RUN apk add --no-cache bash curl ca-certificates \
 && fetch() { curl -fsSL --retry 5 --retry-all-errors --retry-delay 2 "$@"; } \
 && fetch -o /usr/local/bin/kind "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-${TARGETARCH}" \
 && echo "$(fetch "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-${TARGETARCH}.sha256sum" | cut -d' ' -f1)  /usr/local/bin/kind" | sha256sum -c - \
 && fetch -o /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
 && echo "$(fetch "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl.sha256")  /usr/local/bin/kubectl" | sha256sum -c - \
 && chmod +x /usr/local/bin/kind /usr/local/bin/kubectl

COPY kind-config.yaml cluster-up.sh /cluster/
ENTRYPOINT ["bash", "/cluster/cluster-up.sh"]
