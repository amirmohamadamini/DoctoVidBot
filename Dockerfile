# The official Python image. Where Docker Hub can't be reached, set
# PYTHON_IMAGE in .env to a mirror of the same image, such as
# mirror.gcr.io/library/python:3.14-slim or
# public.ecr.aws/docker/library/python:3.14-slim.
ARG PYTHON_IMAGE=python:3.14-slim

FROM ${PYTHON_IMAGE} AS build

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable


# One image, two services (see compose.yaml): `doctovid` runs the bot as user
# doctovid, `doctovid-media` runs ffmpeg as user media. They share only the
# scratch volume, through the group scratch:
#
#   /data           doctovid only      session, database        (bot's volume)
#   /scratch/jobs   doctovid, 2710     one directory per job;   (shared volume)
#                                      media may enter a job's
#                                      directory but not list or
#                                      create them
#   /scratch/sock   media, 2750        the worker's socket
FROM ${PYTHON_IMAGE}

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10003 scratch \
    && useradd --system --uid 10001 --user-group --groups scratch \
        --home-dir /data --shell /usr/sbin/nologin doctovid \
    && useradd --system --uid 10002 --user-group --groups scratch \
        --home-dir /nonexistent --shell /usr/sbin/nologin media \
    && mkdir -m 700 /data && chown doctovid:doctovid /data \
    && mkdir -p /scratch/jobs /scratch/sock \
    && chown doctovid:scratch /scratch/jobs && chmod 2710 /scratch/jobs \
    && chown media:scratch /scratch/sock && chmod 2750 /scratch/sock

COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" DATA_DIR=/data PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

USER doctovid
VOLUME ["/data", "/scratch"]
CMD ["doctovid"]
