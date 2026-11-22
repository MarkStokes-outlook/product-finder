FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/product-finder
COPY pyproject.toml .
COPY src ./src
RUN python -m pip install --no-cache-dir '.[truenas]' \
    && rm -rf /opt/product-finder/build /opt/product-finder/src
COPY deploy/serve.py .

USER 10001:10001
EXPOSE 8080
CMD ["python", "/opt/product-finder/serve.py"]
