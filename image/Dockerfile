# Mystnode image: upstream Mysterium node (myst 1.39.7) + the Umbrel supervisor
# and dashboard. One image, two entrypoints (mystnode.supervisor / mystnode.web).
#
# MUST be built as a multi-arch manifest list for linux/amd64 AND linux/arm64.
# umbrelOS runs on exactly those two arches, the official app linter hard-errors
# on a single-arch image, and compose pins the *index* digest, which only exists
# for a manifest list. Build and publish with:
#
#   docker buildx build \
#     --platform linux/amd64,linux/arm64 \
#     -t ghcr.io/cooperwang-py/mystnode:1.39.7 \
#     --push image/
#   docker buildx imagetools inspect ghcr.io/cooperwang-py/mystnode:1.39.7
#
# Then copy the reported index digest into mystnode-mysterium/docker-compose.yml
# as ghcr.io/cooperwang-py/mystnode:1.39.7@sha256:<index-digest>.
#
# The base is pinned by its index digest too, from:
#   docker buildx imagetools inspect mysteriumnetwork/myst:1.39.7-alpine
FROM mysteriumnetwork/myst:1.39.7-alpine@sha256:9b3cbf9aab301f618be7c2f88b0bcaa0a19ccb6c09c0527dfa5ccad3490c59a6
# python3 (alpine main, 3.12) runs the supervisor and dashboard; py3-bcrypt
# (community, 4.3.0) hashes the NodeUI password the way myst expects.
RUN apk add --no-cache python3 py3-bcrypt
COPY root/ /
ENV PYTHONPATH=/opt/mystnode \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# Upstream ENTRYPOINT is docker-entrypoint.sh; the supervisor calls it itself.
ENTRYPOINT []
CMD ["python3", "-m", "mystnode.web"]
