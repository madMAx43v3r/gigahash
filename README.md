# gigahash.cloud NOCK miners

Official runtime image for the [gigahash.cloud](https://gigahash.cloud/) public NOCK mining pool. One
image contains both the ZK and AI CUDA miners; select the miner with the `zk`
or `ai` command.

## Requirements

- Linux x86-64
- Docker with the NVIDIA Container Toolkit
- NVIDIA driver compatible with CUDA 12.9
- NVIDIA Turing or newer for ZK mining
- NVIDIA Ampere or newer for AI mining

## Pull the image

```bash
docker pull ghcr.io/madmax43v3r/gigahash:1.3
```

Versioned tags are recommended for production. The examples below use `1.3`.

## Run the ZK miner

```bash
docker run -d \
  --name gigahash-zk \
  --restart unless-stopped \
  --gpus all \
  -e PAYOUT_ADDRESS=YOUR_NOCK_ADDRESS \
  -e WORKER_NAME=rig-1 \
  ghcr.io/madmax43v3r/gigahash:1.3 zk
```

## Run the AI miner

```bash
docker run -d \
  --name gigahash-ai \
  --restart unless-stopped \
  --gpus all \
  -e PAYOUT_ADDRESS=YOUR_NOCK_ADDRESS \
  -e WORKER_NAME=rig-1 \
  ghcr.io/madmax43v3r/gigahash:1.3 ai
```

The miners connect to the gigahash.cloud public pool through the built-in DNS
defaults. No pool server option is required.

Follow the miner output with:

```bash
docker logs -f gigahash-zk
docker logs -f gigahash-ai
```

The entrypoint supervises the selected miner and restarts it 10 seconds after
an unexpected exit. `--restart unless-stopped` in the run examples starts the
container again after a host reboot or Docker daemon restart.

## Configuration

| Variable | Meaning |
| --- | --- |
| `PAYOUT_ADDRESS` | Required NOCK payout address |
| `WORKER_NAME` | Pool-visible worker name; defaults to the container hostname |
| `SERVER` | Optional pool or proxy endpoint override |
| `INSTANCES` | Optional instances-per-GPU override |
| `DEVICE` | Use one container-visible CUDA device |
| `DEVICES` | Comma-separated container-visible CUDA device list |
| `PUZZLE` | Alternative to the command: `zk` or `ai` |
| `RESTART_DELAY_SECONDS` | Miner crash-restart delay; defaults to `10` |
| `LOG_FILE` | Optional absolute path receiving a copy of miner output |

Miner command-line options may also be placed after `zk` or `ai`. Explicit
command-line options take precedence over their environment-variable form.

For example, to connect through a local proxy:

```bash
docker run --rm --gpus all \
  --network host \
  ghcr.io/madmax43v3r/gigahash:1.3 zk \
  --server 127.0.0.1:9200 \
  --payout-address YOUR_NOCK_ADDRESS \
  --worker-name rig-1
```

## Select GPUs

Prefer Docker's GPU selection when a container should see only part of a host:

```bash
docker run --rm \
  --gpus '"device=0,1"' \
  -e PAYOUT_ADDRESS=YOUR_NOCK_ADDRESS \
  ghcr.io/madmax43v3r/gigahash:1.3 zk
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
  ghcr.io/madmax43v3r/gigahash:1.3 zk
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
  ghcr.io/madmax43v3r/gigahash:1.3 ai
```

The mounted file is appended across miner and container restarts. Configure
host-side rotation for it if persistent file logging is enabled.

## Verify the image

Version commands do not require GPU access:

```bash
docker run --rm ghcr.io/madmax43v3r/gigahash:1.3 zk --version
docker run --rm ghcr.io/madmax43v3r/gigahash:1.3 ai --version
```

Both commands should report version `1.3`.

Pool status and account statistics are available at
[gigahash.cloud](https://gigahash.cloud/).
