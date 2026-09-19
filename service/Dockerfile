FROM python:3.13-slim
WORKDIR /app
# No COPY pyproject.toml here: pip install below pins its two packages
# directly, never as "pip install ." or "-e .", so pyproject.toml is never
# read at build or run time -- copying it in was dead weight in the image.
# PyYAML>=6 MUST be quoted: RUN uses /bin/sh, where > is a redirection. Without
# the quotes the build silently drops the version constraint and writes pip's
# output to a file literally named "=6" inside /app. Verified by building both ways.
RUN pip install --no-cache-dir paho-mqtt==2.1.0 "PyYAML>=6"
COPY doorbell ./doorbell
# root: the existing archive is written as root by Home Assistant; staying
# consistent keeps both trees readable the same way.
# Documentary only: Traefik still needs the port declared in docker-compose.yml
# labels to route to it, this line does not publish or restrict anything by
# itself. Convention, kept for the next reader.
EXPOSE 8099
CMD ["python", "-m", "doorbell", "/data/config.yaml"]
