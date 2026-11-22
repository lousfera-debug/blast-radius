# Same base image the commits were labeled in (linux/amd64).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
# map.xz: per-test coverage map built at the pinned sqlglot commit (see mapper/).
# src.tar.xz: the pinned commit's sqlglot/ and tests/ python sources, for patch context.
COPY select.py map.xz src.tar.xz /app/
RUN python -c "import tarfile; tarfile.open('/app/src.tar.xz').extractall('/app/src', filter='data')" \
    && rm /app/src.tar.xz \
    && python -m py_compile /app/select.py \
    && chmod -R a+rX /app
ENV BLAST_MAP=/app/map.xz BLAST_SRC=/app/src
CMD ["python", "-I", "/app/select.py"]
