# Channel Emulation for srsRAN

This project runs a software 5G network over live, ray-traced radio channels.
It uses:

- **srsRAN** — a software 5G base station (gNB) and phone (UE).
- **Open5GS** — a software 5G core network.
- **Sionna RT** — NVIDIA's ray tracer, which calculates how a radio signal
  bounces around a scene (walls, reflections, distance) and arrives at the
  receiver.

The supported radio path is SISO: one gNB antenna and one antenna per UE.
The evaluator can run multiple independent SISO UEs, but it does not provide
MIMO channel emulation.

Each trial records ping latency and loss, TCP throughput in both directions,
gNB radio statistics, channel updates, CPU/GPU use, and reproducibility data.

## What you need

- A machine running **Ubuntu 24.04**, or Ubuntu 22.04 with Python 3.11+
  installed separately.
- An **NVIDIA GPU** with recent drivers, CUDA, and the NVIDIA Container
  Toolkit.
- **Python 3.11 or newer**, as required by Sionna 2.0.1.
- Local disk space for the MongoDB subscriber database. The included
  Kubernetes manifest uses a static `hostPath` volume at
  `/var/lib/mongo-pv/datadir-mongodb-0`.

## Setting it up after cloning

```bash
git clone https://github.com/BestCody/Channel-Emulation-Integration-With-SRSran.git sionna-srsran
cd sionna-srsran/srsran_open5gs
```

All the setup commands below run from this `srsran_open5gs/` directory.

Run the repository's local checks before changing the host:

```bash
python3 -m unittest discover -s tests -v
```

**1. Set up the Kubernetes cluster.**
Sets up the single-node cluster and networking.

The installer asks for confirmation because it installs system packages,
disables swap and UFW, restarts container runtimes, and creates host network
bridges. Run it only on a machine dedicated to this testbed. For deliberate
non-interactive installation, pass `--yes`.

```bash
cd testbed-automator
./install.sh
cd ..
```

**2. Enable GPU access in the cluster.**
The UE requests a GPU, but `install.sh` doesn't wire GPU into Kubernetes:

```bash
# Install the NVIDIA Container Toolkit first if this fails.
nvidia-ctk --version

# Add an NVIDIA runtime while retaining runc as the default.
sudo nvidia-ctk runtime configure --runtime=containerd
sudo systemctl restart containerd

# Register the runtime with Kubernetes.
kubectl apply -f - <<'EOF'
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: nvidia
handler: nvidia
EOF

# Install the NVIDIA device plugin.
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.17.1/deployments/static/nvidia-device-plugin.yml
kubectl -n kube-system patch daemonset nvidia-device-plugin-daemonset \
  --type=json -p '[{"op":"add","path":"/spec/template/spec/runtimeClassName","value":"nvidia"}]'
kubectl -n kube-system rollout status \
  daemonset/nvidia-device-plugin-daemonset --timeout=180s

# Confirm that the node advertises at least one GPU.
kubectl get node -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}{"\n"}'
```

**3. Deploy the 5G core and the radio.**
Apply as Kubernetes overlays. The MongoDB overlay includes the static 1 GiB
persistent volume used for subscriber data, so no external storage provisioner
is needed on this single-node testbed. The AMF is capped at 2 GiB so repeated
attachment studies have enough headroom while remaining bounded.

```bash
kubectl create namespace open5gs --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n open5gs -k configs/open5gs/networks5g   # virtual networks
kubectl apply -n open5gs -k configs/open5gs/mongodb       # subscriber database + local PV
kubectl apply -n open5gs -k configs/open5gs/open5gs       # 5G core network
kubectl apply -n open5gs -k configs/srsRAN/srsran-gnb     # base station (gNB)
kubectl apply -n open5gs -k configs/ues/srsue             # phone (UE)

# Recreate existing radio/core pods after CNI installation.
kubectl rollout restart deployment -n open5gs -l app=open5gs
kubectl rollout restart deployment -n open5gs -l app=srsran
kubectl rollout status deployment -n open5gs \
  -l app=open5gs --timeout=300s
kubectl rollout status deployment -n open5gs \
  -l app=srsran --timeout=300s
```

The first Open5GS slice includes an iperf3 server beside its UPF. This keeps
throughput traffic on the real UE data path through the radio and core.

The last command deploys the baseline UE. During an evaluation, the runner
temporarily applies the live-channel UE overlay and restores the baseline UE
afterward.

**4. Create the Python environment for the ray tracing.**

```bash
PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" -c \
  'import sys; assert sys.version_info >= (3, 11), sys.version'
"$PYTHON_BIN" -m venv ~/sionna-env
source ~/sionna-env/bin/activate
python -m pip install --upgrade pip

# This build requires a driver compatible with CUDA 12.8.
python -m pip install "torch==2.11.0" \
  --index-url https://download.pytorch.org/whl/cu128
python -m pip install \
  "sionna==2.0.1" "sionna-rt==2.0.1" pymongo pyzmq numpy

python - <<'PY'
import mitsuba as mi
import sionna
import sionna.rt
import torch

assert sionna.__version__ == "2.0.1"
assert sionna.rt.__version__ == "2.0.1"
assert mi.variant() == "cuda_ad_mono_polarized"
assert torch.cuda.is_available()
print("Sionna RT and CUDA are ready")
PY
```

Use the selector on the [PyTorch installation page](https://pytorch.org/get-started/locally/)
if the host driver does not support the CUDA 12.8 build.

**5. Register the test phones as subscribers.**

The included subscriber and UE authentication values are public laboratory
credentials. Replace them before connecting non-test equipment.

```bash
cd configs/open5gs/mongo-tools
python modify-subscribers.py add
python list-subscribers.py
cd ../../..
```

This registers ten repeatable laboratory subscribers. A one-UE study uses the
first identity; a two-UE study uses the first two identities.

**6. Check that the network and secondary interfaces are ready.**

```bash
kubectl get pods -n open5gs
kubectl exec -n open5gs deployment/open5gs-amf -- \
  ip -brief address show n3
kubectl exec -n open5gs deployment/srsran-gnb -c gnb -- \
  ip -brief address show n3
kubectl exec -n open5gs deployment/srsran-ue1 -c ue -- \
  ip -brief address show n3
```

All deployments must be available, and each interface command must print an
`n3` address. The gNB and UE pods are idle control pods until the evaluator
starts their radio processes.

**7. Build the live-channel UE image.**

```bash
# Build the live UE image.
sudo docker build -t localhost/srsue-live:gr38-v1 -f containers/srsue-live/Dockerfile .
# Import the live image into the Kubernetes image store.
sudo docker save localhost/srsue-live:gr38-v1 | sudo ctr -n k8s.io images import -
sudo ctr -n k8s.io images ls | grep localhost/srsue-live
```

## Running an evaluation

First resolve the study and print its execution plan without changing the
cluster:

```bash
source ~/sionna-env/bin/activate
python3 bin/evaluation-experiment.py plan \
  experiments/studies/live-siso.json
```

Then run the live study:

```bash
python3 bin/evaluation-experiment.py run experiments/studies/live-siso.json \
  --namespace open5gs --confirm-live \
  --condition-set propagation.los=true \
  --condition-set propagation.specular_reflection=true \
  --scene-set scene='"munich"' \
  --scene-set solver.samples_per_src=100000 \
  --profile-set final_ping.count=20
```

On a multi-GPU host, select the GPU used by Sionna RT with the standard CUDA
environment variable, for example `CUDA_VISIBLE_DEVICES=1`.

Results are written to `../results/evaluation/<study>/<run-id>/`, including
per-test tables (CSV), plots (SVG), logs, and the exact resolved settings.

## Ready-made studies

| Study | Purpose | Trials |
|---|---|---:|
| `live-siso.json` | One moving-link smoke test | 1 |
| `multi-ue-validation.json` | Two UEs attach and carry traffic | 1 |
| `network-catalog.json` | Stationary, moving, near, far, and reflected links | 25 |
| `snr-sweep.json` | Five nominal SNR values with five repeats each | 25 |

Run any study with the same command shape:

```bash
python3 bin/evaluation-experiment.py plan \
  experiments/studies/network-catalog.json
python3 bin/evaluation-experiment.py run \
  experiments/studies/network-catalog.json \
  --namespace open5gs --confirm-live
```

Use the dedicated live two-UE check before designing a larger multi-user study:

```bash
python3 bin/evaluation-experiment.py run \
  experiments/studies/multi-ue-validation.json \
  --namespace open5gs --confirm-live
```

## Measurements

Every UE receives an independent SISO channel and produces:

- Continuous and final ping loss and RTT.
- TCP uplink and downlink throughput from iperf3.
- Per-UE IP address, attachment status, and raw traffic output.

The gNB also reports:

- Downlink and uplink MCS.
- BLER derived from successful and failed HARQ/CRC outcomes.
- HARQ NACKs, CRC failures, and retransmission grants.
- Exact PRBs assigned by each downlink and uplink scheduler grant.
- MAC bitrate, buffer occupancy, CQI, and measured uplink SNR.

Raw gNB JSON and scheduler logs remain in each trial. The summarized fields are
written to `summary/trials.csv`, `summary/conditions.json`, and
`summary/ue-results.csv`.

Repeated studies report a two-sided 95% Student t confidence interval for each
mean. Packet loss uses a 95% Wilson score interval over all transmitted ping
packets. An interval for a mean appears only when a condition has at least two
trials.

## Noise and nominal SNR

A condition can select either a fixed complex-noise standard deviation or a
nominal SNR. For a nominal SNR, each channel update uses

`noise_power = sum(|channel_tap|^2) / 10^(SNR_dB/10)`.

This keeps the requested SNR consistent as path power changes. It is a digital
baseband SNR referenced to unit-power IQ samples, not a calibrated RF power at
an antenna connector. The resolved value and every applied noise standard
deviation are stored with the trial.

A **study** is a JSON file describing what to test (for example
`experiments/studies/live-siso.json`). You normally don't edit these by
hand, you override values from the terminal instead, as shown below.

## Terminal options

The evaluator provides four commands:

- `resolve` validates a study and writes its expanded JSON.
- `plan` prints the expanded study and execution plan without changing the
  cluster.
- `run` executes a study and requires `--confirm-live`.
- `summarize` regenerates tables and plots for an existing run.

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
| `noise.snr_db` | Set nominal digital baseband SNR in dB. |
| `noise.sigma` | Set a fixed complex-noise standard deviation. |
| `placement_mode` | Use `random` or the scene's `configured` positions. |
| `placement_seed` | Override the random-placement seed for this condition. |

By default every propagation effect is off; you switch on the ones you want.

Example: `--condition-set propagation.los=true --condition-set propagation.specular_reflection=true`

**`--scene-set` — the physical scene**

| Key | Description |
|---|---|
| `scene` | Which built-in Sionna room to use, e.g. `"box"` or `"munich"`. |
| `transmitter.position` | Base station location as `[x, y, z]` in metres. |
| `receiver.position` | Phone location as `[x, y, z]` in metres. |
| `antenna.pattern` | Antenna shape, e.g. `"iso"` (equal in all directions). |
| `antenna.polarization` | Single-port antenna polarization: `"V"` or `"H"`. |
| `solver.max_depth` | How many bounces to trace (higher = more detail, slower). |
| `solver.samples_per_src` | How many rays to shoot (higher = more accurate, slower). |
| `solver.seed` | Random seed for the ray tracing. |

Example: `--scene-set scene='"munich"' --scene-set 'transmitter.position=[-1.5,0,2]'`

**`--profile-set` — how the test is measured**

| Key | Description |
|---|---|
| `final_ping.count` | How many ping packets to send at the end of the test. |
| `final_ping.deadline_seconds` | How long to wait for those pings before giving up. |
| `final_ping.interval_seconds` | Delay between final ping packets. |
| `attachment_timeout_seconds` | How long to wait for the phone to connect before failing. |
| `amf_interval_seconds` | How often to record the core network's memory use. |
| `resource_interval_seconds` | How often to record CPU and GPU use. |
| `throughput.enabled` | Enable uplink and downlink iperf3 tests. |
| `throughput.duration_seconds` | Measured seconds per direction and UE. |
| `throughput.omit_seconds` | Warm-up time excluded from throughput. |

Every override you use is recorded in the results folder, so a run can always be
reproduced.

## Repository layout

- `srsran_open5gs/channel_emulation` contains the Sionna RT controller.
- `srsran_open5gs/configs` contains the Open5GS, gNB, and UE overlays.
- `srsran_open5gs/containers` builds the live-channel UE image.
- `srsran_open5gs/experiment_framework` runs and records studies.
- `srsran_open5gs/experiments` contains reusable studies and conditions.
- `srsran_open5gs/gr-sionna-channel` implements the GNU Radio channel block.

## License

This repository is available under the [MIT License](LICENSE).
