# AgentCore Runtime requires linux/arm64. Build with Finch/Docker buildx.
# Slim base + non-root user (least privilege; no shell tools needed at runtime).
# Pinned to a patch version AND its immutable digest so builds are reproducible and a
# new upstream patch cannot silently change the image. To update: pull the new tag,
# read its digest (`finch image inspect <tag> --format '{{index .RepoDigests 0}}'`),
# then bump both the tag and the digest here.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12.13-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Normalise the mode explicitly. COPY preserves the BUILDER's file permissions, so a
# developer whose local server.py is 0600 (restrictive umask, some editors) produces an
# image the non-root user below cannot read — the runtime then fails at start with
# "python: can't open file '/app/server.py': [Errno 13] Permission denied".
RUN chmod 0644 /app/server.py /app/requirements.txt

# Drop root: the container only needs to listen on :8000 and make outbound HTTPS.
RUN useradd --create-home --uid 1001 appuser
USER appuser

EXPOSE 8000
CMD ["python", "server.py"]
