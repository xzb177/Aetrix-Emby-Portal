# Aetrix Emby Portal

[简体中文](README.md) | **English**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/xzb177/Aetrix-Portal/actions/workflows/ci.yml/badge.svg)](https://github.com/xzb177/Aetrix-Portal/actions/workflows/ci.yml)
[![Docker](https://img.shields.io/badge/docker-compose-2496ED.svg?logo=docker&logoColor=white)](docs/deploy-docker.md)

A self-hosted movie & TV media server that fully implements the Emby protocol (`/emby/*`). Third-party Emby clients connect to it directly — no official Emby Server required.

Python 3.12 + FastAPI · Vue 3 + TypeScript · PostgreSQL · Redis 7 · ffmpeg

---

## What This Is

Aetrix Emby Portal is a self-hosted media server. The backend fully implements the Emby protocol, so clients like Infuse, SenPlayer, Forward, and the official Emby apps can connect to it as if it were an Emby Server — while the server side is provided entirely by this project, with zero dependence on the official Emby Server.

The system is split into two processes by responsibility: **EM** (the portal: user site / admin console / library scanning / membership billing) and **EA** (the Emby protocol gateway: authentication / browsing / streaming / transcoding / subtitles). Both share the same PostgreSQL and the same `SECRET_KEY`. The default layout runs both in one machine (single container set), but you can also split them across machines for a distributed deployment.

Built for technical users who run their own server, their own media library, and their own users.

---

## Architecture

| Component | Responsibility |
| --- | --- |
| **EM** | Portal: user site, admin console, library scanning, membership billing |
| **EA** | Emby protocol gateway: authentication, browsing, streaming, transcoding, subtitles |
| **PostgreSQL** | Metadata, users, billing data (default) |
| **Redis 7** | Cache, sessions, task queue |
| **ffmpeg** | HLS transcoding |

```
Emby clients ──HTTP──▶ EA (streaming)
Browser ──▶ EM (portal) ──┴──▶ PostgreSQL ──▶ your disks
                           └──▶ Redis
```

EM and EA must be configured with the same `SECRET_KEY`. The default layout is a single-machine, single-container set; you can point at remote PostgreSQL / Redis instances via environment variables for a split deployment — see [docs/deploy-ea.md](docs/deploy-ea.md).

---

## Quick Start

Requires Docker and Docker Compose v2.

```bash
git clone https://github.com/xzb177/Aetrix-Portal.git
cd Aetrix-Portal
cp env.example .env
```

Edit `.env` — at minimum, fill in these two:

```bash
# Generate a strong value; EM and EA must use the same one
python3 -c "import secrets; print(secrets.token_urlsafe(48))"

SECRET_KEY=<the generated value>
POSTGRES_PASSWORD=<your database password>
```

Start:

```bash
./scripts/deploy.sh
```

`scripts/deploy.sh` is for first-time deployment and brings up the following services:

| Service | Description |
| --- | --- |
| `aetrix-api` | EM portal API and web |
| `aetrix-ea` | EA Emby protocol gateway |
| `aetrix-worker` | Background jobs: scanning, scraping, transcoding |
| `postgres` | Database |
| `redis` | Cache and queue |

For later updates, use `scripts/update.sh` — it backs up the database and configuration automatically before pulling new images.

Verify:

```bash
curl http://127.0.0.1:8000/api/health
```

Admin console: `http://<IP>:8000/admin/`

---

## Configuration

Core environment variables below; the full list lives in `env.example`.

| Variable | Required | Description |
| --- | --- | --- |
| `SECRET_KEY` | Yes | ≥32 characters, must be identical across EM and EA |
| `POSTGRES_PASSWORD` | Yes | PostgreSQL password |
| `POSTGRES_USER` / `POSTGRES_DB` | No | Database user and name, provided by compose by default |
| `DATABASE_TYPE` | No | Defaults to `postgresql`; `sqlite` for test scenarios |
| `REDIS_URL` | No | Redis connection string, defaults to the built-in `redis` service |
| `MEDIA_ROOT` | No | Host media directory, mounted into containers read-only |
| `STRM_MOUNT_DIR` | No | rclone / Drive mount point for `.strm` direct-play scenarios |
| `TMDB_API_KEY` | No | Set to enable TMDB scraping |
| `AETRIX_PORT` | No | Host-facing port, default `8000` (change this to change ports, not the container-internal `PORT`) |

Example media directory mounts (host paths specified in `.env`):

```bash
MEDIA_ROOT=/srv/media
STRM_MOUNT_DIR=/mnt/rclone
```

`MEDIA_ROOT` is mounted read-only by default — the services never write to your media directory.

---

## Features

### Media Library

- Recognizes common episode naming schemes: `S01E02`, `1x02`, `第N集`, etc.
- ffprobe-based video / audio / subtitle track detection
- TMDB metadata and poster scraping
- Incremental scanning — only changed files are processed

### Playback & Streaming

- HTTP Range direct streaming with seek support
- ffmpeg HLS transcoding
- Full Emby protocol compatibility, including `PlaybackInfo`, subtitle delivery, and progress reporting
- `.strm` direct play (Google Drive / rclone mounts)

### Users & Operations

- JWT-based user accounts
- Memberships, points, redemption codes, invitations

---

## Development

Prerequisites: Python 3.12, Node.js, npm.

```bash
# Backend dependencies
pip install -r backend/requirements.txt

# User-facing frontend
cd user_frontend && npm ci && npm run dev
```

Tests (sqlite by default, no PostgreSQL needed):

```bash
DATABASE_TYPE=sqlite pytest tests/ -q
```

CI has 5 required checks — all must pass before merging:

1. User frontend type-check + build
2. Admin frontend type-check + build
3. Backend smoke tests
4. Deploy self-check
5. PostgreSQL smoke tests

PR flow: push to a branch → open a PR → CI all green → merge.

---

## Documentation

| Document | Content |
| --- | --- |
| [docs/README.md](docs/README.md) | Docs hub — start here |
| [docs/deploy-docker.md](docs/deploy-docker.md) | docker compose deployment |
| [docs/deploy-em.md](docs/deploy-em.md) | EM portal deployment & configuration |
| [docs/deploy-ea.md](docs/deploy-ea.md) | EA gateway deployment & split deployment |
| [docs/operations.md](docs/operations.md) | Operations, backups, and troubleshooting |
| [docs/capabilities.md](docs/capabilities.md) | Full capability list |
| [docs/performance.md](docs/performance.md) | Performance and tuning |

Changelog: [CHANGELOG.md](CHANGELOG.md).

---

## Buy Me a Coffee

This project was built from scratch by a single developer with an AI team — scraping, organizing, and playback, all-in-one, all open source here.

If it helped you, or if you believe it has a future, you can buy the developer a coffee to keep this project going.

<img src="docs/assets/coffee-qr.jpg" width="300" alt="WeChat appreciation code">

---

## License

MIT
