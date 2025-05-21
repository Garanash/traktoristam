# Используем официальный образ Python
FROM python:3.9-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем все необходимые файлы
COPY config.py parser_last_posts.py searcher.py ./
COPY files ./files/

# Устанавливаем зависимости (если они есть, добавьте requirements.txt)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Устанавливаем supervisord для управления процессами
RUN apt-get update && apt-get install -y supervisor && \
    rm -rf /var/lib/apt/lists/*

# Конфигурация supervisord
RUN mkdir -p /var/log/supervisor
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Запускаем supervisord
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]