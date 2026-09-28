# syntax=docker/dockerfile:1
#
# uc-archiver, containerised.
#
# Two things this image deliberately does NOT contain, and both are explained
# where they are missed:
#
#   * WinRAR / rar. Creating a .rar needs the `rar` binary, which is
#     proprietary and not redistributable, so it is not baked in. Pass
#     --archiver /path/to/rar (mount one in) or build with --build-arg, or use
#     7-Zip, which is free and is installed here. A .rar release is what this
#     project normally produces, so on Linux you will want to mount one.
#
#   * a browser of your own. scrapling drives its own patched Firefox, which
#     `scrapling install` fetches at build time. That is the browser that
#     clears the Cloudflare challenge, and it is the only reason this image is
#     as large as it is.

FROM python:3.12-slim

# 7-Zip for reading the .7z shares when no rar is mounted, and the shared
# libraries scrapling's browser needs. curl is only for the healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      7zip p7zip-full ca-certificates curl \
      libgtk-3-0 libdbus-glib-1-2 libasound2 libxtst6 xauth \
 && rm -rf /var/lib/apt/lists/*

# rar, when the builder has one to hand. COPY fails if it is not there, so
# this is opt-in: build with --build-arg RAR_FROM=/path/to/rar.
ARG RAR_FROM=""
COPY ${RAR_FROM} /usr/local/bin/rar
RUN chmod +x /usr/local/bin/rar || true

WORKDIR /app

# The tool itself is one file and needs no install step.
COPY uc_archiver.py selftest.py ./
COPY profiles/ ./profiles/

# scrapling pulls curl_cffi and the parser; the [all] extra brings the browser
# engines. Pinned loosely on purpose: the browser is the part most likely to
# need a bump when the challenge changes.
RUN pip install --no-cache-dir "scrapling[all]>=0.4.15" \
 && scrapling install

# The archives here run to tens of gigabytes, and the container's writable
# layer is not the place for that. /work is a volume by default.
VOLUME ["/work"]

ENV PYTHONUNBUFFERED=1 \
    WORK_DIR=/work \
    OUTPUT_DIR=/work/out

# Reports whether the browser this needs is actually usable, which is the one
# thing that silently fails otherwise.
HEALTHCHECK --interval=1m --timeout=20s --retries=2 \
  CMD python -c "import sys, uc_archiver as u; sys.exit(1 if u.scrapling_problem() else 0)" \
   || exit 1

ENTRYPOINT ["python", "/app/uc_archiver.py"]
CMD ["--help"]
