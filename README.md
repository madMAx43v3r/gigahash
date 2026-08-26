# gigahash.cloud NOCK miners

Official runtime image for the [gigahash.cloud](https://gigahash.cloud/) public NOCK mining pool. One
image contains both the ZK and AI CUDA miners; select `zk`, `ai`, or the
profitability-switching `auto` mode.

## Requirements

- Linux x86-64
- Docker with the NVIDIA Container Toolkit
- NVIDIA driver compatible with CUDA 12.9
- NVIDIA Turing or newer for ZK mining
- NVIDIA Ampere or newer for AI mining
- NVIDIA Ampere or newer for auto mode

## Pull the image

```bash
docker pull ghcr.io/madmax43v3r/gigahash:latest
```

The `latest` tag follows the newest release. Pin a versioned tag when you need
a reproducible deployment.

## Run the ZK miner

```bash
docker run -d \
  --name gigahash-zk \
  --restart unless-stopped \
  --gpus all \
  -e PAYOUT_ADDRESS=YOUR_NOCK_ADDRESS \
  -e WORKER_NAME=rig-1 \
  ghcr.io/madmax43v3r/gigahash:latest zk
```

## Run the AI miner

```bash
docker run -d \
  --name gigahash-ai \
  --restart unless-stopped \
  --gpus all \
  -e PAYOUT_ADDRESS=YOUR_NOCK_ADDRESS \
  -e WORKER_NAME=rig-1 \
  ghcr.io/madmax43v3r/gigahash:latest ai
```

## Switch automatically by profitability

```bash
docker run -d \
  --name gigahash-auto \
  --restart unless-stopped \
  --gpus all \
  -e PAYOUT_ADDRESS=YOUR_NOCK_ADDRESS \
  -e WORKER_NAME=rig-1 \
  ghcr.io/madmax43v3r/gigahash:latest auto
```

Auto mode detects the installed GPU models and sends them to the pool API. The
API classifies them by NVIDIA architecture series and compares ZK and AI using
the measured profile for that series. It checks again every 60 seconds and
switches only when the other puzzle is more than 5% better than the running
puzzle. If model detection fails, the API uses its backward-compatible RTX 5090
profile. If the API is unavailable, it keeps the running miner; on a first start
without API data it falls back to ZK.

Add `-e AUTO_HYSTERESIS_PERCENT=10` to the Docker command to use a 10%
switching threshold instead.

The miners connect to the gigahash.cloud public pool through the built-in DNS
defaults. No pool server option is required. For a specific pool host,
`--server HOST` defaults to port `9100` for ZK and `9101` for AI. To connect
through a standalone proxy, use `--proxy HOST`; its default port is `9200`.
In auto mode, leave `SERVER` unset unless the override is a shared proxy
endpoint that accepts both ZK and AI miners.

Follow the miner output with:

```bash
docker logs -f gigahash-zk
docker logs -f gigahash-ai
docker logs -f gigahash-auto
```

The entrypoint supervises the selected miner and restarts it 10 seconds after
an unexpected exit. `--restart unless-stopped` in the run examples starts the
container again after a host reboot or Docker daemon restart.

## Run on Vast.ai

The public image can run directly from a custom
[Vast.ai template](https://cloud.vast.ai/templates/). Configure the template
with these values:

| Setting | Value |
| --- | --- |
| Image Path:Tag | `ghcr.io/madmax43v3r/gigahash:latest` |
| Launch Mode | `docker ENTRYPOINT` |
| Entrypoint Arguments | `zk`, `ai`, or `auto` |
| Ports | None |
| Docker Repository Authentication | None |

Add these rows in the template's **Environment Variables** table:

| Variable | Value |
| --- | --- |
| `PAYOUT_ADDRESS` | Your NOCK payout address (required) |
| `WORKER_NAME` | Optional override; omit it to use the Vast.ai instance ID |
| `AUTO_HYSTERESIS_PERCENT` | Optional auto-mode switching threshold; defaults to `5` |

Do not add `WORKER_NAME` when you want the Vast.ai instance ID as the worker
name. The image detects it automatically. Add `WORKER_NAME` only to override
that default.

Select an `amd64` offer with `cuda_max_good >= 12.9`. Use a Turing-or-newer
GPU for ZK mining or an Ampere-or-newer GPU for AI mining. Vast.ai assigns the
rented GPU to the container automatically, so no Docker GPU option is needed.

Use an on-demand instance when uninterrupted uptime matters; interruptible
instances may be paused. The image does not include SSH or Jupyter, so inspect
miner output through the instance logs. A successful test should show a pool
connection followed by at least one accepted share.

Vast.ai's SSH and Jupyter launch modes replace the image entrypoint, so use
`docker ENTRYPOINT` unless you arrange to start the miner separately.

## Configuration

| Variable | Meaning |
| --- | --- |
| `PAYOUT_ADDRESS` | Required NOCK payout address |
| `WORKER_NAME` | Pool-visible worker name; defaults to the Vast.ai instance ID when available, otherwise the container hostname |
| `SERVER` | Optional pool or proxy endpoint override |
| `INSTANCES` | Optional instances-per-GPU override |
| `DEVICE` | Use one container-visible CUDA device |
| `DEVICES` | Comma-separated container-visible CUDA device list |
| `PUZZLE` | Alternative to the command: `zk`, `ai`, or `auto` |
| `AUTO_HYSTERESIS_PERCENT` | Auto-mode switching threshold from `0` to `100`; defaults to `5` |
| `AUTO_POLL_SECONDS` | Auto-mode profitability polling interval; defaults to `60` |
| `AUTO_PROFITABILITY_URL` | Auto-mode pool profitability endpoint override |
| `RESTART_DELAY_SECONDS` | Miner crash-restart delay; defaults to `10` |
| `LOG_FILE` | Optional absolute path receiving a copy of miner output |

Miner command-line options may also be placed after the mode. Explicit
command-line options take precedence over their environment-variable form.
In auto mode, use `AI_DENSE_K` instead of the AI-only `--dense-k` option.

For example, to connect through a local proxy:

```bash
docker run --rm --gpus all \
  --network host \
  ghcr.io/madmax43v3r/gigahash:latest zk \
  --proxy 127.0.0.1 \
  --payout-address YOUR_NOCK_ADDRESS \
  --worker-name rig-1
```

## Select GPUs

Prefer Docker's GPU selection when a container should see only part of a host:

```bash
docker run --rm \
  --gpus '"device=0,1"' \
  -e PAYOUT_ADDRESS=YOUR_NOCK_ADDRESS \
  ghcr.io/madmax43v3r/gigahash:latest zk
```

Device numbers passed to `DEVICE` or `DEVICES` refer to the devices visible
inside the container.

## Optional persistent logs

Docker captures miner output by default. Limit Docker's local log growth with:

```bash
docker run -d \
  --name gigahash-zk \
  --restart unless-stopped \
  --gpus all \
  --log-opt max-size=50m \
  --log-opt max-file=3 \
  -e PAYOUT_ADDRESS=YOUR_NOCK_ADDRESS \
  ghcr.io/madmax43v3r/gigahash:latest zk
```

To additionally write a host-visible log file, mount a directory and set
`LOG_FILE`:

```bash
docker run -d \
  --name gigahash-ai \
  --restart unless-stopped \
  --gpus all \
  -v /var/log/gigahash:/logs \
  -e LOG_FILE=/logs/miner.log \
  -e PAYOUT_ADDRESS=YOUR_NOCK_ADDRESS \
  ghcr.io/madmax43v3r/gigahash:latest ai
```

The mounted file is appended across miner and container restarts. Configure
host-side rotation for it if persistent file logging is enabled.

## Verify the image

Version commands do not require GPU access:

```bash
docker run --rm ghcr.io/madmax43v3r/gigahash:latest zk --version
docker run --rm ghcr.io/madmax43v3r/gigahash:latest ai --version
```

Both commands should report the current release version.

Pool status and account statistics are available at
[gigahash.cloud](https://gigahash.cloud/).
