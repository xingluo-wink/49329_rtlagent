ARG BASE_IMAGE=fpgachina-rtlhls@sha256:a7cf627e11708e8a6dcd9de689ff934f7b450b79369ebc7bb0217e8b54d81e56
FROM ${BASE_IMAGE}
WORKDIR /opt/rtlagent
COPY agent/ agent/
COPY skill/ skill/
COPY serve/ serve/
COPY model/ model/
COPY tools/ tools/
COPY licenses/ licenses/
COPY docs/ docs/
COPY selftest/ selftest/
COPY examples/ examples/
COPY .gitattributes .gitignore .dockerignore Dockerfile requirements.txt README.md REPORT.md manifest.json checksums.json release.json run.sh run_baseline.sh baseline.py ./
# Fail if the offline base lacks a dependency; no startup-time downloads.
RUN /opt/python/bin/python3 -B -c "import uvicorn; assert tuple(map(int,uvicorn.__version__.split('.')[:2])) >= (0,29)" && \
    /opt/python/bin/python3 -B tools/check_release.py && \
    chmod +x run.sh run_baseline.sh serve/start.sh serve/run_api.sh
ENV PYTHONDONTWRITEBYTECODE=1 TRACK=rtl MODEL_NAME=qwen36-35b-a3b \
    LLM_BASE_URL=http://127.0.0.1:8000/v1 EDA_TMP=/var/tmp/eda AGENT_TMP=/var/tmp/rtlagent-work
# Weights are prepared/bind-mounted before offline evaluation as documented
# in model/MODEL.md. start.sh captures this session's identity automatically.
# The file interface needs no tournament token or public HTTP service.
# Only the submission API is published through the organizer's tunnel.
EXPOSE 7860
CMD ["/bin/bash"]
