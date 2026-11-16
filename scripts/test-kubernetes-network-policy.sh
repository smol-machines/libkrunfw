#!/bin/sh
# Run against an owned development cluster after booting the candidate kernel.
# KUBECTL may name a kubectl binary or a symlink to the K3s multicall binary.
set -eu

KUBECTL=${KUBECTL:-kubectl}
namespace="smol-policy-$(date +%s)-$$"
k() { "$KUBECTL" "$@"; }

# Install the cleanup trap only after creating our own namespace successfully.
k create namespace "$namespace"
trap 'k delete namespace "$namespace" --wait=false >/dev/null' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

k run server -n "$namespace" --image=nginx:alpine
k run client -n "$namespace" --image=busybox:1.37 --command -- sleep 600
k wait -n "$namespace" --for=condition=Ready pod --all --timeout=120s
server_ip=$(k get pod server -n "$namespace" -o jsonpath='{.status.podIP}')
test -n "$server_ip"

reachable() {
    k exec -n "$namespace" client -- wget -q -T 2 -O /dev/null "http://$server_ip" >/dev/null 2>&1
}

expect_connectivity() {
    expected=$1
    label=$2
    attempts=0
    while [ "$attempts" -lt 20 ]; do
        actual=blocked
        if reachable; then actual=allowed; fi
        if [ "$actual" = "$expected" ]; then
            echo "PASS: $label ($actual)"
            return 0
        fi
        attempts=$((attempts + 1))
        sleep 1
    done
    echo "FAIL: $label: expected $expected, observed $actual" >&2
    k get pods,networkpolicies -n "$namespace" -o wide >&2
    return 1
}

expect_connectivity allowed baseline
k apply -n "$namespace" -f - <<'YAML'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: deny-ingress
spec:
  podSelector:
    matchLabels:
      run: server
  policyTypes: [Ingress]
YAML
expect_connectivity blocked deny-ingress

k apply -n "$namespace" -f - <<'YAML'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-client
spec:
  podSelector:
    matchLabels:
      run: server
  policyTypes: [Ingress]
  ingress:
  - from:
    - podSelector:
        matchLabels:
          run: client
    ports:
    - protocol: TCP
      port: 80
YAML
expect_connectivity allowed allow-client-selector

k apply -n "$namespace" -f - <<'YAML'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: deny-egress
spec:
  podSelector:
    matchLabels:
      run: client
  policyTypes: [Egress]
YAML
expect_connectivity blocked deny-egress

k apply -n "$namespace" -f - <<YAML
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-server-ip
spec:
  podSelector:
    matchLabels:
      run: client
  policyTypes: [Egress]
  egress:
  - to:
    - ipBlock:
        cidr: $server_ip/32
    ports:
    - protocol: TCP
      port: 80
YAML
expect_connectivity allowed allow-server-ipblock
