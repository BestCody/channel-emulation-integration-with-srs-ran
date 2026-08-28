#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ASSUME_YES=false

usage() {
  cat <<'EOF'
Usage: ./install.sh [--yes]

Set up a single-node Kubernetes testbed for the srsRAN/Open5GS integration.

  --yes   Accept the host-level changes without an interactive prompt.
EOF
}

for argument in "$@"; do
  case "$argument" in
    --yes)
      ASSUME_YES=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $argument" >&2
      usage >&2
      exit 2
      ;;
  esac
done

cecho() {
  local color_name="$1"
  local message="$2"
  local red="\033[0;31m"
  local green="\033[0;32m"
  local yellow="\033[1;33m"
  local reset="\033[0m"
  local color="$reset"

  case "$color_name" in
    RED) color="$red" ;;
    GREEN) color="$green" ;;
    YELLOW) color="$yellow" ;;
  esac
  printf '%b%s %b\n' "$color" "$message" "$reset"
}

confirm_host_changes() {
  cat <<'EOF'
This installer makes host-level changes. It will:
  - install system packages and package repositories;
  - disable swap and comment swap entries in /etc/fstab;
  - disable UFW when it is installed and active;
  - configure and restart containerd and Docker;
  - create a single-node Kubernetes cluster and Open vSwitch bridges.

Run this only on a machine intended to host the testbed.
EOF

  if "$ASSUME_YES"; then
    return
  fi
  if [ ! -t 0 ]; then
    echo "Interactive confirmation is required; rerun with --yes for automation." >&2
    exit 1
  fi

  local response
  read -r -p "Continue? [y/N] " response
  case "$response" in
    y|Y|yes|YES) ;;
    *)
      echo "Installation cancelled."
      exit 0
      ;;
  esac
}

timer_sec() {
  local seconds="$1"
  while [ "$seconds" -gt 0 ]; do
    printf 'Waiting for %s seconds ...\r' "$seconds"
    sleep 1
    seconds=$((seconds - 1))
  done
  printf '\n'
}

install_packages() {
  sudo apt-get update
  sudo apt-get install -y \
    ca-certificates curl git gnupg iproute2 iputils-ping \
    python3-pip python3-venv python3-virtualenv tcpdump tmux vim
}

disable_swap() {
  cecho GREEN "Disabling swap for Kubernetes ..."
  if [ -n "$(swapon --noheadings --show 2>/dev/null)" ]; then
    sudo swapoff -a
    sudo sed -i '/[[:space:]]swap[[:space:]]/ s/^[[:space:]]*[^#]/#&/' /etc/fstab
    echo "Swap has been disabled and its /etc/fstab entries were commented out."
  else
    echo "Swap is already disabled."
  fi
}

disable_firewall() {
  if ! command -v ufw >/dev/null 2>&1; then
    echo "UFW is not installed; no firewall change was made."
    return
  fi
  if sudo ufw status | grep -q '^Status: active'; then
    cecho YELLOW "Disabling UFW for the single-node testbed ..."
    sudo ufw disable
  else
    echo "UFW is already inactive."
  fi
}

configure_docker_repository() {
  sudo install -m 0755 -d /etc/apt/keyrings
  if [ ! -s /etc/apt/keyrings/docker.gpg ]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
      | sudo gpg --dearmor --yes --output /etc/apt/keyrings/docker.gpg
  fi
  sudo chmod a+r /etc/apt/keyrings/docker.gpg

  local architecture
  local codename
  architecture="$(dpkg --print-architecture)"
  codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"
  echo "deb [arch=${architecture} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${codename} stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
}

install_container_runtime() {
  if ! command -v containerd >/dev/null 2>&1 || ! command -v docker >/dev/null 2>&1; then
    cecho GREEN "Installing Docker and containerd ..."
    configure_docker_repository
    sudo apt-get update
    sudo apt-get install -y \
      containerd.io docker-buildx-plugin docker-ce docker-ce-cli docker-compose-plugin
  else
    cecho YELLOW "Docker and containerd are already installed."
  fi

  command -v containerd >/dev/null
  command -v docker >/dev/null
  sudo mkdir -p /etc/containerd
  if [ ! -s /etc/containerd/config.toml ]; then
    containerd config default | sudo tee /etc/containerd/config.toml >/dev/null
  fi
  sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
  sudo systemctl enable containerd docker
  sudo systemctl restart containerd docker

  sudo systemctl is-active --quiet containerd
  sudo systemctl is-active --quiet docker
  cecho GREEN "Docker and containerd are running."
}

setup_k8s_networking() {
  cecho GREEN "Setting up Kubernetes networking ..."
  cat <<'EOF' | sudo tee /etc/modules-load.d/k8s.conf >/dev/null
overlay
br_netfilter
EOF
  sudo modprobe overlay
  sudo modprobe br_netfilter

  cat <<'EOF' | sudo tee /etc/sysctl.d/k8s.conf >/dev/null
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward = 1
EOF
  sudo sysctl --system >/dev/null
}

install_k8s() {
  if command -v kubectl >/dev/null 2>&1 \
    && command -v kubeadm >/dev/null 2>&1 \
    && command -v kubelet >/dev/null 2>&1; then
    cecho YELLOW "Kubernetes components are already installed."
    return
  fi

  cecho GREEN "Installing Kubernetes v1.29 components ..."
  sudo apt-get update
  sudo apt-get install -y apt-transport-https ca-certificates curl gpg
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.29/deb/Release.key \
    | sudo gpg --dearmor --yes --output /etc/apt/keyrings/kubernetes-apt-keyring.gpg
  echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.29/deb/ /' \
    | sudo tee /etc/apt/sources.list.d/kubernetes.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y kubeadm kubectl kubelet
  sudo apt-mark hold kubeadm kubectl kubelet
}

create_k8s_cluster() {
  if [ -f /etc/kubernetes/admin.conf ]; then
    cecho YELLOW "A Kubernetes cluster already exists; skipping cluster creation."
    return
  fi

  cecho GREEN "Creating the Kubernetes cluster ..."
  sudo kubeadm init --config "$SCRIPT_DIR/kubeadm-config.yaml"
  mkdir -p "${HOME}/.kube"
  sudo cp /etc/kubernetes/admin.conf "${HOME}/.kube/config"
  sudo chown "$(id -u):$(id -g)" "${HOME}/.kube/config"

  timer_sec 60
  local taints
  taints="$(kubectl get nodes -o jsonpath='{range .items[*].spec.taints[*]}{.key}={.effect}{"\n"}{end}')"
  if grep -qx 'node-role.kubernetes.io/control-plane=NoSchedule' \
      <<<"$taints"; then
    kubectl taint nodes --all \
      node-role.kubernetes.io/control-plane:NoSchedule-
  fi
}

install_cni() {
  if kubectl get pods -n kube-flannel -l app=flannel 2>/dev/null | grep -q '1/1'; then
    cecho YELLOW "Flannel is already running."
    return
  fi

  cecho GREEN "Installing Flannel ..."
  kubectl apply -f https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml
  timer_sec 60
  kubectl wait pods -n kube-flannel -l app=flannel \
    --for=condition=Ready --timeout=120s
}

install_multus() {
  if kubectl get pods -n kube-system -l app=multus 2>/dev/null | grep -q '1/1'; then
    cecho YELLOW "Multus is already running."
    return
  fi

  cecho GREEN "Installing Multus ..."
  local build_root="$SCRIPT_DIR/build"
  local multus_checkout="$build_root/multus-cni"
  mkdir -p "$build_root"
  if [ -d "$multus_checkout/.git" ]; then
    git -C "$multus_checkout" pull --ff-only
  else
    git clone https://github.com/k8snetworkplumbingwg/multus-cni.git "$multus_checkout"
  fi
  kubectl apply -f "$multus_checkout/deployments/multus-daemonset-thick.yml"
  timer_sec 30
  kubectl wait pods -n kube-system -l app=multus \
    --for=condition=Ready --timeout=120s
}

install_helm() {
  if command -v helm >/dev/null 2>&1; then
    local helm_version
    helm_version="$(helm version --short)"
    if [[ "$helm_version" == *"v3"* ]]; then
      cecho YELLOW "Helm 3 is already installed."
      return
    fi
    echo "An unsupported Helm version is installed: $helm_version" >&2
    exit 1
  fi

  cecho GREEN "Installing Helm 3 ..."
  curl -fsSL https://baltocdn.com/helm/signing.asc \
    | sudo gpg --dearmor --yes --output /usr/share/keyrings/helm.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/helm.gpg] https://baltocdn.com/helm/stable/debian/ all main" \
    | sudo tee /etc/apt/sources.list.d/helm-stable-debian.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y helm
}

setup_ovs_cni() {
  if ! command -v ovs-vsctl >/dev/null 2>&1; then
    cecho GREEN "Installing Open vSwitch ..."
    sudo apt-get update
    sudo apt-get install -y openvswitch-switch
  else
    cecho YELLOW "Open vSwitch is already installed."
  fi

  cecho GREEN "Creating the testbed Open vSwitch bridges ..."
  sudo ovs-vsctl --may-exist add-br n2br
  sudo ovs-vsctl --may-exist add-br n3br
  sudo ovs-vsctl --may-exist add-br n4br

  cecho GREEN "Installing the OVS CNI operator ..."
  local operator_base="https://github.com/kubevirt/cluster-network-addons-operator/releases/download/v0.89.1"
  kubectl apply -f "$operator_base/namespace.yaml"
  kubectl apply -f "$operator_base/network-addons-config.crd.yaml"
  kubectl apply -f "$operator_base/operator.yaml"
  kubectl apply -f https://gist.githubusercontent.com/niloysh/1f14c473ebc08a18c4b520a868042026/raw/d96f07e241bb18d2f3863423a375510a395be253/network-addons-config.yaml
  timer_sec 30
  kubectl wait networkaddonsconfig cluster \
    --for=condition=Available --timeout=180s
}

confirm_host_changes
install_packages
disable_swap
disable_firewall
setup_k8s_networking
install_container_runtime
install_k8s
create_k8s_cluster
install_cni
install_multus
install_helm
setup_ovs_cni

cecho GREEN "Single-node testbed setup completed."
