FROM python:3.13-alpine AS base
RUN apk upgrade --no-cache \
    && python3 -m pip uninstall --yes pip setuptools wheel \
    && rm -rf /usr/local/lib/python3.13/ensurepip /root/.cache
WORKDIR /app

FROM base AS test
COPY kerit_status.py poll.js test_kerit_status.py ./
RUN python3 -I -B -m unittest -v

FROM base AS final
COPY --from=test /app/kerit_status.py /app/kerit_status.py
COPY --from=test /app/poll.js /app/poll.js
USER 10001:10001
EXPOSE 8000
CMD ["python3", "-I", "-B", "-u", "kerit_status.py"]
