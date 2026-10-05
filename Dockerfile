# Image du backend : le bot (`tradeagent run`) et son interface en lecture seule (`tradeagent web`).
# Une seule image, un conteneur par processus : voir compose.yaml.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Un utilisateur sans droits : le conteneur n'écrit que dans /app/data (monté depuis le serveur).
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin tradeagent

WORKDIR /app
COPY docker/constraints.txt /tmp/constraints.txt
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --constraint /tmp/constraints.txt . && rm -rf /tmp/constraints.txt build src/*.egg-info

# La config fait partie de l'image : la changer, c'est reconstruire (et changer la mise demande un reset).
COPY config.yaml ./config.yaml
RUN mkdir /app/data && chown tradeagent:tradeagent /app/data
USER tradeagent

ENTRYPOINT ["tradeagent"]
CMD ["status", "--all"]
