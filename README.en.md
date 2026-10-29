# Aetrix Emby Portal

[简体中文](README.md) | **English**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/xzb177/Aetrix-Emby-Portal/actions/workflows/ci.yml/badge.svg)](https://github.com/xzb177/Aetrix-Emby-Portal/actions/workflows/ci.yml)
[![Docker](https://img.shields.io/badge/docker-compose-2496ED.svg?logo=docker&logoColor=white)](docs/deploy-docker.md)

A self-hosted movie & TV media server that implements the commonly used Emby client protocol surface (`/emby/*`). Third-party Emby clients connect to it directly — no official Emby Server required. Playback is direct link or relayed passthrough only; there is no server-side transcoding.

Python 3.12 + FastAPI · Vue 3 + TypeScript · PostgreSQL · Redis 7 · ffmpeg

---

## What This Is

Aetrix Emby Portal is a self-hosted media server. The backend implements the Emby endpoints clients commonly rely on (authentication, browsing, `PlaybackInfo`, streaming, subtitles, progress reporting), so clients like Infuse, SenPlayer, Forward, and the official Emby apps can connect to it as if it were an Emby Server — while the server side is provided entirely by this project, with zero dependence on the official Emby Server. The server does not transcode (`SupportsTranscoding` is always false), so formats that would need transcoding must be decoded by the client.

The system is split into two processes by responsibility: **EM** (the portal: user site / admin console / library scanning / membership billing) and **EA** (the Emby protocol gateway: authentication / browsing / streaming / subtitles). Both share the same PostgreSQL and the same `SECRET_KEY`. The default layout is a single machine (the self-built compose splits api / ea / worker into separate containers; the licensed compose merges them into one), but you can also split them across machines for a distributed deployment.

Built for technical users who run their own server, their own media library, and their own users.

---

## Architecture

| Component | Responsibility |
| --- | --- |
| **EM** | Portal: user site, admin console, library scanning, membership billing |
| **EA** | Emby protocol gateway: authentication, browsing, streaming (direct link / relay passthrough), subtitles |
| **PostgreSQL** | Metadata, users, billing data (default) |
| **Redis 7** | Cache, sessions, task queue |
| **ffmpeg** | Auxiliary only: embedded text subtitle extraction, thumbnail frames, etc. (no video transcoding) |

```
Emby clients ──HTTP──▶ EA (streaming)
Browser ──▶ EM (portal) ──┴──▶ PostgreSQL ──▶ your disks
                           └──▶ Redis
```

EM and EA must be configured with the same `SECRET_KEY`. The default layout is a single machine; you can point at remote PostgreSQL / Redis instances via environment variables for a split deployment — see [docs/deploy-ea.md](docs/deploy-ea.md).

---

## Quick Start

Requires Docker and Docker Compose v2.

```bash
git clone https://github.com/xzb177/Aetrix-Emby-Portal.git
cd Aetrix-Emby-Portal
cp env.example .env
```

Edit `.env` — at minimum, fill in these two:

```bash
# Generate a strong value; EM and EA must use the same one
python3 -c "import secrets; print(secrets.token_urlsafe(48))"

SECRET_KEY=<the generated value>
POSTGRES_PASSWORD=<your database password>
```

Start the backend:

```bash
./scripts/deploy.sh
```

`scripts/deploy.sh` is for first-time deployment and brings up the following services:

| Service | Description |
| --- | --- |
| `aetrix-api` | EM portal API (API only — it does not serve web pages) |
| `aetrix-ea` | EA Emby protocol gateway |
| `aetrix-worker` | Maintenance, metadata enrichment, expiry reminders, scheduled scans, new-release watching, Redis scan queue |
| `postgres` | Database |
| `redis` | Cache and queue |

The web frontend (user site + admin console) is a separate container, `aetrix-web`
(nginx, host port 8081), and is not started by the script above. To get the web UI,
bring up both compose files together:

```bash
docker compose -f docker-compose.yml -f docker-compose.frontend.yml up -d
```

Pick one route and don't mix them: the scripts use `--remove-orphans`, which will
remove `aetrix-web` because it is not in the default compose file. Full guide:
[docs/deploy-docker.md](docs/deploy-docker.md).

For later updates, `scripts/update.sh` does a `git pull`, rebuilds, and restarts the
backend services. It only attempts an SQLite backup — on the default PostgreSQL
deployment, run `scripts/backup_db.sh` first.

Verify:

```bash
curl http://127.0.0.1:8000/api/health
```

Admin console: `http://<IP>:8081/admin/` (once `aetrix-web` is up)

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
| `TMDB_API_KEY` | No | Set to enable TMDB scraping |
| `AETRIX_PORT` | No | Host-facing port of the licensed compose (`docker-compose.prod.yml`) only, default `8000`; the self-built compose hardcodes its ports (backend 8000, web 8081) in the compose files — change them there |

Example media directory mounts (host paths specified in `.env`):

```bash
MEDIA_ROOT=/srv/media
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
- No server-side transcoding: only two delivery modes, direct link (302 to the drive URL, bytes never touch the server) and relayed passthrough (the server forwards bytes as-is)
- Compatible with the Emby protocol surface clients commonly use, including `PlaybackInfo`, subtitle delivery, and progress reporting; `PlaybackInfo` always reports no transcoding support, and HLS transcode URLs return 404
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
