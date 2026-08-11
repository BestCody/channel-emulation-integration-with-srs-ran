# Channel Emulation for srsRAN

This project lets you run a 5G network and test it
over **realistic radio channels** that are computed with ray tracing. It uses the tools:

- **srsRAN** — a software 5G base station (gNB) and phone (UE).
- **Open5GS** — a software 5G core network.
- **Sionna RT** — NVIDIA's ray tracer, which calculates how a radio signal
  bounces around a scene (walls, reflections, distance) and arrives at the
  receiver.

## What you need

- A machine running **Ubuntu 22.04 or 24.04**.
- An **NVIDIA GPU** with recent drivers, CUDA, and the NVIDIA Container
  Toolkit
- **Python 3.11 or newer** (`sionna` and `numpy` require it)
- Local disk space for the MongoDB subscriber database. The included
  Kubernetes manifest uses a static `hostPath` volume at
  `/var/lib/mongo-pv/datadir-mongodb-0`; **Longhorn is not installed or
  required**.

## Setting it up after cloning

```bash
git clone https://github.com/BestCody/channel-emulation-integration-with-srs-ran sionna-srsran
cd sionna-srsran/srsran_open5gs
```

All the setup commands below run from this `srsran_open5gs/` directory.

**1. Set up the Kubernetes cluster.**
Sets up the single-node cluster and networking.

```bash
cd testbed-automator
./install.sh
cd ..
```

**2. Enable GPU access in the cluster.**
The UE requests a GPU, but `install.sh` doesn't wire GPU into Kubernetes:

```bash
# Needs the NVIDIA Container Toolkit (provides nvidia-ctk). If `nvidia-ctk
# --version` fails, install it first:
#   https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html

# a. add an "nvidia" runtime to containerd, leaving runc as the default
sudo nvidia-ctk runtime configure --runtime=containerd
sudo systemctl restart containerd

# b. register that runtime with Kubernetes
kubectl apply -f - <<'EOF'
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: nvidia
handler: nvidia
EOF

# c. install the NVIDIA device plugin
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.17.1/deployments/static/nvidia-device-plugin.yml
kubectl -n kube-system patch daemonset nvidia-device-plugin-daemonset \
  --type=json -p '[{"op":"add","path":"/spec/template/spec/runtimeClassName","value":"nvidia"}]'

# d. confirm the node now advertises GPUs (prints 1 or more)
kubectl get node -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}{"\n"}'
```

**3. Deploy the 5G core and the radio.**
Apply as Kubernetes overlays. The MongoDB overlay includes the static 1 GiB
persistent volume used for subscriber data, so no external storage provisioner
is needed on this single-node testbed.

The MongoDB manifests retain `storageClassName: longhorn` as a **legacy matching
label** so existing deployments such as `atlas-gpu01` remain compatible. The
volume itself is a normal Kubernetes `hostPath` volume, not a Longhorn volume.
Do not delete an existing MongoDB PVC/PV just to rename that label; doing so can
remove the saved Open5GS subscriber database.

```bash
kubectl create namespace open5gs --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n open5gs -k configs/open5gs/networks5g   # virtual networks
kubectl apply -n open5gs -k configs/open5gs/mongodb       # subscriber database + local PV
kubectl apply -n open5gs -k configs/open5gs/open5gs       # 5G core network
kubectl apply -n open5gs -k configs/srsRAN/srsran-gnb     # base station (gNB)
kubectl apply -n open5gs -k configs/ues/srsue             # phone (UE)
```

Register the phone as a subscriber:

```bash
python3 -m pip install pymongo
cd configs/open5gs/mongo-tools
python3 modify-subscribers.py add
python3 list-subscribers.py
cd ../../..
```

**4. Create the Python environment for the ray tracing.**

```bash
python3 -m venv ~/sionna-env
source ~/sionna-env/bin/activate
pip install "sionna==2.0.1" "sionna-rt==2.0.1" pyzmq numpy

# PyTorch must match your NVIDIA driver's CUDA version — check the "CUDA
# Version" shown top-right in `nvidia-smi`. A plain `pip install torch` may
# grab a build too new for your driver (torch then reports CUDA unavailable).
# For a CUDA 12.x driver, install a cu12 build explicitly:
pip install "torch==2.11.0" --index-url https://download.pytorch.org/whl/cu128
# Other driver versions: https://pytorch.org/get-started/locally/
```

**5. Check that the network pods are running.**

```bash
kubectl get pods -n open5gs
```

The Open5GS core, gNB, and UE pods should all be `Running`.

**6. Build the live-channel UE image.**

```bash
# base image with the sparse channel block
sudo docker build -t localhost/srsue-sparse:gr38-v1 -f containers/srsue-channel/Dockerfile .
# live image built on top of it
sudo docker build -t localhost/srsue-live:gr38-v1 -f containers/srsue-live/Dockerfile .
# make the live image visible to the cluster's containerd
sudo docker save localhost/srsue-live:gr38-v1 | sudo ctr -n k8s.io images import -
```

## Running an evaluation

```bash
source ~/sionna-env/bin/activate   

#Example Run
python3 bin/evaluation-experiment.py run experiments/studies/neural-base.json \
  --namespace open5gs --confirm-live \
  --condition-set propagation.los=true \
  --condition-set propagation.specular_reflection=true \
  --scene-set scene='"munich"' \
  --scene-set 'receiver.velocity=[10,0,0]' \
  --profile-set final_ping.count=20
```

Results are written to `results/evaluation/<study>/<run-id>/`, including per-test
tables (CSV), plots (SVG), the logs, and a copy of the exact settings used.

A **study** is a JSON file describing what to test (for example
`experiments/studies/neural-base.json`). You normally don't edit these by
hand, you override values from the terminal instead, as shown below.

## Terminal options

Command-specific flags are noted in the table.

| Option | What it does |
|---|---|
| `<study file>` | Path to the study JSON to run (required, first argument). |
| `--namespace` | (`run` only) The Kubernetes namespace the network runs in (usually `open5gs`). |
| `--confirm-live` | (`run` only) Confirms you understand it will start the radio and change the cluster. Without it, nothing runs. |
| `--parameters FILE` | Load extra settings from another JSON file. Can be given more than once. |
| `--output FILE` | (`resolve` only) Where to write the checked, fully-expanded config. |

### Changing settings from the terminal

You can change the following values from terminal when evaluating:

**`--set` — run and network settings**

| Key | Description |
|---|---|
| `radio.ue_number` | How many phones (UEs) to simulate at once. |
| `trials_per_condition` | How many times to repeat each test. |
| `scene.randomize_positions` | `true` places the antennas randomly; `false` uses the fixed positions in the scene. |
| `scene.placement_seed` | The random seed for placement, so a random run can be repeated exactly. |
| `scene.min_link_distance_m` | Smallest allowed distance (in metres) between transmitter and receiver when placing them randomly. |

Example: `--set radio.ue_number=2 --set trials_per_condition=3`

**`--condition-set` — what channel to test**

| Key | Description |
|---|---|
| `propagation.los` | Turn on the direct line-of-sight path. |
| `propagation.specular_reflection` | Turn on mirror-like reflections off flat surfaces. |
| `propagation.diffuse_reflection` | Turn on scattered reflections off rough surfaces. |
| `propagation.refraction` | Turn on signal bending through materials. |
| `propagation.diffraction` | Turn on bending around edges. |
| `throughput.status` | `deferred` (throughput measurement is not run in-trial). |

By default every propagation effect is off; you switch on the ones you want.

Example: `--condition-set propagation.los=true --condition-set propagation.specular_reflection=true`

**`--scene-set` — the physical scene**

| Key | Description |
|---|---|
| `scene` | Which built-in Sionna room to use, e.g. `"box"` or `"munich"`. |
| `transmitter.position` | Base station location as `[x, y, z]` in metres. |
| `receiver.position` | Phone location as `[x, y, z]` in metres. |
| `receiver.velocity` | Phone velocity as `[vx, vy, vz]` in m/s. Adds Doppler to the channel. Default `[0,0,0]` (stationary). |
| `antenna.pattern` | Antenna shape, e.g. `"iso"` (equal in all directions). |
| `antenna.polarization` | Antenna polarization: `"V"` or `"H"`. `"cross"` is **not supported right now** — it makes a dual-port (2×2) antenna, but the streaming channel is single-antenna (SISO), so it fails with a coefficient/delay shape mismatch. |
| `solver.max_depth` | How many bounces to trace (higher = more detail, slower). |
| `solver.samples_per_src` | How many rays to shoot (higher = more accurate, slower). |
| `solver.seed` | Random seed for the ray tracing. |

Example: `--scene-set scene='"munich"' --scene-set 'transmitter.position=[-1.5,0,2]'`

**`--profile-set` — how the test is measured**

| Key | Description |
|---|---|
| `final_ping.count` | How many ping packets to send at the end of the test. |
| `final_ping.deadline_seconds` | How long to wait for those pings before giving up. |
| `attachment_timeout_seconds` | How long to wait for the phone to connect before failing. |
| `amf_interval_seconds` | How often to record the core network's memory use. |
| `resource_interval_seconds` | How often to record CPU and GPU use. |

Every override you use is recorded in the results folder, so a run can always be
reproduced.
