FROM python:3.9-slim as build

ENV PYTHONUNBUFFERED=1
WORKDIR /opt/app

COPY . .
RUN pip install .

FROM python:3.9-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /root

COPY --from=build /usr/local/lib/python3.9/site-packages /usr/local/lib/python3.9/site-packages
COPY --from=build /usr/local/bin/autoagora-sidecar /usr/local/bin/autoagora-sidecar

ENTRYPOINT [ "autoagora-sidecar" ]
