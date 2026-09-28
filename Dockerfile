# PTH-660 web flasher: static site served by nginx.
# WebHID runs in the user's browser (Chrome/Edge) and talks to the tablet directly;
# the container never touches USB.

# ---- test stage: node unit tests (package parser, checksums vs the Python reference)
FROM node:20-alpine AS test
WORKDIR /app
COPY web ./web
COPY firmware ./firmware
COPY tests ./tests
RUN node --test tests/

# ---- runtime: nginx serving the app and the firmware packages
FROM nginx:1.27-alpine AS web
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY web /usr/share/nginx/html
COPY firmware /srv/firmware
# the test stage must have passed for the image to build
COPY --from=test /app/tests/golden.json /tmp/tests-passed
EXPOSE 80
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD wget -qO- http://127.0.0.1/firmware/manifest.json >/dev/null || exit 1
