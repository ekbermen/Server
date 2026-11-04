FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8

# ---- Cekirdek diller: Python, C, C++, Java, Go, Rust, PHP, Ruby, Perl, Lua, SQLite, Node, Bash ----
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl unzip util-linux \
      python3 gcc g++ libc6-dev \
      default-jdk-headless \
      golang-go rustc \
      php-cli ruby perl lua5.4 sqlite3 \
      nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# ---- Opsiyonel: TypeScript (tsx). Kurulamazsa sunucu yine calisir, TypeScript listede gorunmez ----
RUN npm install -g tsx || echo "tsx kurulamadi, atlandi"

# ---- Opsiyonel: C# (Mono) ----
RUN apt-get update \
    && (apt-get install -y --no-install-recommends mono-mcs mono-runtime libmono-system-core4.0-cil \
        || echo "mono kurulamadi, atlandi") \
    && rm -rf /var/lib/apt/lists/*

# ---- Opsiyonel: Dart ----
RUN (curl -fsSL https://storage.googleapis.com/dart-archive/channels/stable/release/latest/sdk/dartsdk-linux-x64-release.zip -o /tmp/dart.zip \
     && unzip -q /tmp/dart.zip -d /opt \
     && ln -s /opt/dart-sdk/bin/dart /usr/local/bin/dart \
     && rm -f /tmp/dart.zip) || echo "dart kurulamadi, atlandi"

# ---- Opsiyonel: Kotlin (cok bellek ister; ucretsiz 512 MB planda onerilmez). INSTALL_KOTLIN=1 ile ac ----
ARG INSTALL_KOTLIN=0
RUN if [ "$INSTALL_KOTLIN" = "1" ]; then \
      curl -fsSL https://github.com/JetBrains/kotlin/releases/download/v1.9.24/kotlin-compiler-1.9.24.zip -o /tmp/k.zip \
      && unzip -q /tmp/k.zip -d /opt && ln -s /opt/kotlinc/bin/kotlinc /usr/local/bin/kotlinc && rm -f /tmp/k.zip \
      || echo "kotlin kurulamadi, atlandi"; \
    fi

# Kod, yetkisiz bir kullanici olarak calistirilir (uid 10001)
RUN useradd -u 10001 -M -s /usr/sbin/nologin runner || true

WORKDIR /app
COPY server.py /app/server.py

ENV PORT=10000
EXPOSE 10000
CMD ["python3", "/app/server.py"]
