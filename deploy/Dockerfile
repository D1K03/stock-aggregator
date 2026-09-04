# Single stage. `psycopg[binary]` and `httpx` ship wheels, so there is no build
# toolchain to discard and a builder stage would add a layer of indirection to
# save nothing.
FROM python:3.11-slim

# The commit this image was built from. A container has no git history, so this
# is the only way `/status` and `scoring_run.git_sha` can know what is running.
# Passed by the deploy workflow as --build-arg GIT_SHA=${{ github.sha }}.
ARG GIT_SHA=""
ENV SCREENER_GIT_SHA=$GIT_SHA

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependency metadata first, so a change to source does not reinstall psycopg.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# `screener.boot` resolves migrations relative to the working directory, so
# they ship as files rather than package data — they are SQL the operator reads
# and diffs, not something importable.
COPY migrations ./migrations

# Non-root. Nothing here writes to disk — the database has its own volume and
# secrets stay in the environment — so the filesystem can stay owned by someone
# else.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin screener
USER 10001

EXPOSE 8080

CMD ["python", "-m", "screener.boot"]
